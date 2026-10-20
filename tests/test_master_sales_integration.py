"""Master QR sales use real isolated DB linkage, never client role hints."""
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.v1.routes import orders, qr_payment as qr_routes
from app.core.database import get_db_session
from app.dependencies.auth import get_inventory_poster
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.fiscal import FiscalDocument
from app.models.order import OrderHandover, OrderPayment
from app.models.product import Product
from app.schemas.fiscal import MockShiftCommand
from app.schemas.order import POSOrderCreate, OrderHandoverCreate
from app.schemas.qr_payment import QRCreate, QRSimulate
from app.services import fiscal, fiscal_returns, local_sales, qr_payment, shipments
from app.services.auth import AuthService
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product
from tests.test_qr_payment_integration import flags

pytestmark = pytest.mark.anyio


async def setup_sale(database):
    product = await add_product(database, stock=5)
    async with database() as session:
        admin = await session.get(AdminUser, 1)
        await fiscal.shift(session, admin, fiscal.REGISTER,
            MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
        for user_id in (2, 3):
            session.add(AdminUser(id=user_id, email=f'master-{user_id}@example.test', hashed_password='unused',
                is_active=True, is_superuser=False, can_record_sales=True, can_accept_qr_payments=True,
                can_post_inventory=True, can_fiscalize_sales=True, can_fiscalize_returns=True,
                can_manage_fiscal_settings=True, can_manage_shipments=True))
            session.add(Master(admin_user_id=user_id, full_name=f'Synthetic master {user_id}'))
        await session.commit()
        master = await session.get(AdminUser, 2)
        # The narrow flow must not depend on broad stock-posting permission.
        master.can_post_inventory = False
        await session.commit()
        payload = POSOrderCreate(items=[{'product_id': product.id, 'quantity': 1}], simulation=True)
        # Historical simulations stay readable; masters cannot create new ones.
        own = await local_sales.prepare(session, admin, payload, str(uuid4()))
        row = await local_sales.load(session, own.id)
        row.created_by_user_id = 2
        other = await local_sales.prepare(session, admin, payload, str(uuid4()))
        row = await local_sales.load(session, other.id)
        row.created_by_user_id = 3
        await session.commit()
        attempt = await qr_payment.create(session, master, own.id,
            QRCreate(expected_revision=own.revision, cash_register_id=fiscal.REGISTER, confirm=True), str(uuid4()))
        foreign = await qr_payment.create(session, await session.get(AdminUser, 3), other.id,
            QRCreate(expected_revision=other.revision, cash_register_id=fiscal.REGISTER, confirm=True), str(uuid4()))
        token = AuthService().issue_token_pair(master).access_token
        return product.id, own, other, attempt, foreign, token


async def test_master_api_scopes_orders_qr_receipts_and_denies_alternate_commands(database, checkout_dependencies):
    _, own, other, attempt, foreign, token = await setup_sale(database)
    app = FastAPI()
    app.include_router(orders.backoffice_router, prefix='/orders')
    app.include_router(qr_routes.backoffice_router)
    async def db():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = db
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://local.test',
            headers={'Authorization': 'Bearer ' + token}) as client:
        listed = await client.get('/orders')
        assert listed.status_code == 200, listed.text
        assert [row['id'] for row in listed.json()['items']] == [own.id]
        for path in (f'/orders/{other.id}', f'/orders/{other.id}/fulfillment',
                f'/orders/{other.id}/qr-payment-attempts', f'/qr-payment-attempts/{foreign.id}',
                f'/qr-payment-attempts/{foreign.id}/receipt'):
            assert (await client.get(path)).status_code == 404, path
        own_response = await client.get(f'/orders/{own.id}')
        assert own_response.status_code == 200
        assert own_response.json()['sales_actions']['can_record_payment'] is False
        assert own_response.json()['shipment_actions'] == {}
        assert own_response.json()['sales_actions']['can_handover'] is False
        current = await client.get(f'/qr-payment-attempts/{attempt.id}')
        assert current.status_code == 200
        assert current.json()['allowed_actions'] == ['refresh', 'cancel']
        operations = [
            ('/orders', {'items': [{'product_id': own.items[0].product_id, 'quantity': 1}],
                'simulation': True}, 'POST', 403),
            (f'/qr-payment-attempts/{attempt.id}/simulate', {'expected_revision': attempt.revision,
                'event_id': str(uuid4()), 'outcome': 'success'}, 'POST', 403),
            (f'/orders/{own.id}/payments', {'expected_revision': 2, 'amount_minor': 10000,
                'method': 'cash', 'source': 'operator_confirmation', 'evidence': 'synthetic'}, 'POST', 403),
            (f'/orders/{own.id}/status', {'status': 'cancelled'}, 'PATCH', 403),
            (f'/orders/{own.id}/preliminary-documents', {'expected_revision': 2}, 'POST', 403),
            (f'/qr-payment-attempts/{foreign.id}/refresh', {'expected_revision': foreign.revision}, 'POST', 404),
            (f'/qr-payment-attempts/{foreign.id}/cancel', {'expected_revision': foreign.revision}, 'POST', 404),
            (f'/orders/{other.id}/qr-payment-attempts', {'expected_revision': 2,
                'cash_register_id': fiscal.REGISTER, 'confirm': True}, 'POST', 404),
        ]
        for path, payload, method, status in operations:
            response = await client.request(method, path, json=payload, headers={'Idempotency-Key': str(uuid4())})
            assert response.status_code == status, (path, response.text)
        # The same bearer token must immediately lose access after server revocation.
        async with database() as session:
            master = await session.get(AdminUser, 2)
            master.can_accept_qr_payments = False
            await session.commit()
        assert (await client.get(f'/qr-payment-attempts/{attempt.id}')).status_code == 403
        assert (await client.get(f'/orders/{own.id}')).status_code == 403
        assert (await client.get('/orders')).status_code == 403
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        master = await session.get(AdminUser, 2)
        master.can_post_inventory = True
        await session.commit()
        for operation in (lambda: fiscal.readiness(session, master),
                lambda: fiscal.require(session, master),
                lambda: fiscal_returns.require(session, master, mutation=False),
                lambda: fiscal_returns.create_return(session, master, own.id, None, str(uuid4())),
                lambda: fiscal_returns.record_refund(session, master, 1, None, str(uuid4())),
                lambda: get_inventory_poster(master, session),
                lambda: shipments.require(session, master)):
            with pytest.raises(HTTPException) as denied:
                await operation()
            assert denied.value.status_code == 403


@pytest.mark.parametrize('outcome', ['success_receipt_pending', 'success'])
async def test_master_handover_needs_canonical_issued_qr_and_preserves_stock(database, checkout_dependencies, outcome):
    product_id, own, _, attempt, _, _ = await setup_sale(database)
    async with database() as session:
        admin = await session.get(AdminUser, 1)
        paid = await qr_payment.command(session, admin, attempt.id,
            QRSimulate(expected_revision=attempt.revision, event_id=uuid4(), outcome=outcome), str(uuid4()), 'simulate')
        master = await session.get(AdminUser, 2)
        current = await local_sales.load(session, own.id)
        response = await local_sales.response(current, session, master)
        assert response.sales_actions['can_handover'] is (outcome == 'success')
        payload = OrderHandoverCreate(expected_revision=current.revision, evidence='synthetic handover')
        if outcome != 'success':
            with pytest.raises(HTTPException):
                await local_sales.command(session, master, own.id, payload, str(uuid4()), handover=True)
        else:
            receipt = await qr_routes.receipt(attempt.id, session, master)
            assert receipt.status_code == 200 and b'MOCK' in receipt.body
            # A projected paid/issued label cannot bypass durable receipt validation.
            document = await session.get(FiscalDocument, paid.fiscal_document_id)
            original = document.snapshot_hash
            document.snapshot_hash = 'corrupt'
            await session.flush()
            with pytest.raises(HTTPException):
                await local_sales.command(session, master, own.id, payload, str(uuid4()), handover=True)
            document.snapshot_hash = original
            await session.flush()
            key = str(uuid4())
            completed = await local_sales.command(session, master, own.id, payload, key, handover=True)
            assert completed.status.value == 'completed'
            assert await local_sales.command(session, master, own.id, payload, key, handover=True) == completed
        product = await session.get(Product, product_id)
        expected = (4, 1) if outcome == 'success' else (5, 2)
        assert (product.stock_quantity, product.reserved_quantity) == expected
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == (outcome == 'success')
