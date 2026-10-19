"""Synthetic, isolated local PostgreSQL proofs for stage 5 facts and capabilities."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.fiscal import (FiscalDocument, MockRefund, MockReturnReceipt, ReturnRecord,
    MockFiscalReport, MockFiscalShare, MockReceipt, FiscalEvent)
from app.models.inventory import InventoryMovement, MovementType
from app.models.order import Order, OrderItem, OrderPayment
from app.models.product import Product
from app.schemas.fiscal import FiscalPreviewCreate, MockShiftCommand
from app.schemas.fiscal_returns import (ReturnCreate, MockRefundCreate, ReturnReceiptCreate,
    MockReportCreate, ReconciliationAckCreate, FiscalShareCreate, FiscalShareRevoke)
from app.schemas.order import POSOrderCreate, OrderPaymentCreate, OrderHandoverCreate
from app.services import fiscal, fiscal_returns as returns, local_sales
from tests.test_fiscal_integration import actor, preview, submit, approval, command, mock_flags
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product

pytestmark = pytest.mark.anyio


async def issued_sale(database, *, quantity=3, completed=True, additional_discount=0, item_name=None):
    product = await add_product(database, stock=max(10, quantity))
    async with database() as session:
        user = await actor(session)
        order = await local_sales.prepare(session, user, POSOrderCreate(items=[{'product_id': product.id, 'quantity': quantity}], simulation=True), str(uuid4()))
        if additional_discount:
            row = await session.get(Order, order.id)
            row.discount_amount += Decimal(additional_discount) / 100
            row.total_amount -= Decimal(additional_discount) / 100
            await session.commit()
            order = await local_sales.load(session, order.id)
        if item_name:
            item = await session.get(OrderItem, order.items[0].id)
            item.product_name = item_name
            await session.commit()
        amount = fiscal.exact_minor(order.total_amount)
        order = await local_sales.command(session, user, order.id, OrderPaymentCreate(expected_revision=order.revision,
            amount_minor=amount, method='card', source='operator_confirmation', evidence='synthetic stage5 payment'), str(uuid4()))
        if completed:
            order = await local_sales.command(session, user, order.id, OrderHandoverCreate(expected_revision=order.revision,
                evidence='synthetic stage5 handover'), str(uuid4()), handover=True)
        register = await fiscal.cash_register(session)
        if not register:
            await fiscal.shift(session, user, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
    doc = await submit(database, order.id, approval(await preview(database, order.id)))
    issued = await command(database, doc, 'process')
    return order, issued


async def create(database, order, sale, quantity=1, key=None):
    async with database() as session:
        return await returns.create_return(session, await actor(session), order.id, ReturnCreate(parent_sale_id=sale.id,
            items=[{'order_item_id': sale.snapshot['items'][0]['order_item_id'], 'quantity': quantity}], reason='synthetic partial return'), key or str(uuid4()))


async def refund(database, row, key=None):
    async with database() as session:
        return await returns.record_refund(session, await actor(session), row.id, MockRefundCreate(evidence='synthetic refund recorded'), key or str(uuid4()))


async def receive(database, row, *, disposition='restock', key=None):
    async with database() as session:
        return await returns.record_receipt(session, await actor(session), row.id, ReturnReceiptCreate(items=[
            {'order_item_id': i.order_item_id, 'quantity': i.quantity, 'disposition': disposition} for i in row.items],
            evidence='synthetic received goods'), key or str(uuid4()))


async def return_preview(database, row):
    async with database() as session:
        return await returns.create_preview(session, await actor(session), row.id, FiscalPreviewCreate(cash_register_id=fiscal.REGISTER), str(uuid4()))


async def test_partial_return_rounding_discount_and_original_truth(database, checkout_dependencies):
    order, sale = await issued_sale(database, additional_discount=1)
    first = await create(database, order, sale)
    second = await create(database, order, sale)
    third = await create(database, order, sale)
    assert [r.amount_minor for r in (first, second, third)] == [9999, 10000, 10000]
    assert sum(r.amount_minor for r in (first, second, third)) == sale.snapshot['amount_minor']
    with pytest.raises(HTTPException) as error:
        await create(database, order, sale)
    assert error.value.detail['code'] == 'return_capacity_exceeded'
    async with database() as session:
        row = await session.get(OrderItem, sale.snapshot['items'][0]['order_item_id'])
        row.product_name = 'later catalog/order edit'
        row.price = 999
        await session.commit()
    refunded = await refund(database, first)
    assert refunded.refund.amount_minor == first.amount_minor
    p = await return_preview(database, first)
    assert p.kind == 'return' and p.snapshot['items'][0]['name'] == sale.snapshot['items'][0]['name']
    doc = await submit(database, order.id, approval(p))
    issued = await command(database, doc, 'process', mock_delivery_outcome='failed')
    assert issued.kind == 'return' and issued.status == 'issued' and issued.delivery.status == 'failed'
    assert issued.parent_sale_id == sale.id and issued.return_id == first.id and issued.provider_uuid != sale.provider_uuid
    await command(database, issued, 'delivery')
    async with database() as session:
        original = await fiscal.get_document(session, await actor(session), sale.id)
        assert original.snapshot == sale.snapshot and original.revision == sale.revision
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(MockRefund)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReturnReceipt)) == 0


async def test_return_same_different_key_races_and_refund_restock_once(database, checkout_dependencies):
    order, sale = await issued_sale(database, quantity=2)
    key = str(uuid4())
    first, replay = await asyncio.gather(create(database, order, sale, key=key), create(database, order, sale, key=key))
    assert first == replay
    async def reserve():
        try:
            return await create(database, order, sale)
        except HTTPException as error:
            assert error.detail['code'] == 'return_capacity_exceeded'
    results = await asyncio.gather(reserve(), reserve())
    assert sum(r is not None for r in results) == 1
    key = str(uuid4())
    a, b = await asyncio.gather(refund(database, first, key), refund(database, first, key))
    assert a == b and a.refund.method == 'card'
    with pytest.raises(HTTPException) as error:
        await refund(database, first)
    assert error.value.detail['code'] == 'refund_exists'
    key = str(uuid4())
    a, b = await asyncio.gather(receive(database, first, key=key), receive(database, first, key=key))
    assert a == b
    with pytest.raises(HTTPException) as error:
        await receive(database, first)
    assert error.value.detail['code'] == 'return_receipt_exists'
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(ReturnRecord)) == 2
        assert await session.scalar(select(func.count()).select_from(MockRefund)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReturnReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.movement_type == MovementType.customer_return)) == 1
        product = await session.get(Product, sale.snapshot['items'][0]['product_id'])
        assert product.stock_quantity == 9 and product.reserved_quantity == 0


async def test_fiscal_unknown_cancel_reapprove_does_not_repeat_other_facts(database, checkout_dependencies):
    order, sale = await issued_sale(database)
    row = await create(database, order, sale)
    with pytest.raises(HTTPException) as error:
        await return_preview(database, row)
    assert error.value.detail['code'] == 'mock_refund_required'
    await refund(database, row)
    await receive(database, row)
    doc = await submit(database, order.id, approval(await return_preview(database, row)))
    cancelled = await command(database, doc, 'cancel')
    assert cancelled.status == 'cancelled'
    doc2 = await submit(database, order.id, approval(await return_preview(database, row), mock_outcome='timeout_after_acceptance'))
    unknown = await command(database, doc2, 'process')
    assert unknown.status == 'unknown'
    issued = await command(database, unknown, 'process')
    assert issued.status == 'issued' and issued.provider_uuid == unknown.provider_uuid
    with pytest.raises(HTTPException):
        await create(database, order, sale, quantity=3)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockRefund)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReturnReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 2


async def test_unshipped_return_cannot_restock_and_not_restock_is_separate(database, checkout_dependencies):
    order, sale = await issued_sale(database, completed=False)
    row = await create(database, order, sale)
    with pytest.raises(HTTPException) as error:
        await receive(database, row)
    assert error.value.detail['code'] == 'handover_required'
    await receive(database, row, disposition='not_restock')
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockRefund)) == 0
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.movement_type == MovementType.customer_return)) == 0


async def test_permissions_flag_gate_and_origin_revocation(database, checkout_dependencies, monkeypatch):
    order, sale = await issued_sale(database)
    row = await create(database, order, sale)
    async with database() as session:
        user = await actor(session)
        user.is_superuser = False
        user.can_record_sales = True
        user.can_fiscalize_sales = True
        await session.commit()
    await refund(database, row)
    with pytest.raises(HTTPException) as error:
        await return_preview(database, row)
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        await receive(database, row)
    assert error.value.status_code == 403
    async with database() as session:
        user = await actor(session)
        user.can_fiscalize_returns = True
        await session.commit()
    doc = await submit(database, order.id, approval(await return_preview(database, row)))
    async with database() as session:
        user = await actor(session)
        user.can_fiscalize_returns = False
        session.add(AdminUser(id=2, email='mock-worker@example.test', hashed_password='unused', is_active=True, can_fiscalize_returns=True))
        await session.commit()
    async with database() as session:
        result = await fiscal.process_job(session, await session.get(AdminUser, 2), doc.id)
        assert result.status == 'error'
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', False)
    with pytest.raises(HTTPException) as error:
        await create(database, order, sale)
    assert error.value.detail['code'] == 'mock_fiscal_disabled'


async def test_reconciliation_ack_stays_visible_reports_z_race(database, checkout_dependencies):
    order, sale = await issued_sale(database)
    row = await create(database, order, sale)
    async with database() as session:
        user = await actor(session)
        state = await returns.reconciliation(session, user)
        assert state.totals.sales_minor == 30000 and state.totals.reserved_returns_minor == 10000
        assert state.buckets['refund_pending'][0].resource_id == str(row.id)
        body = ReconciliationAckCreate(case_type='refund_pending', resource_id=str(row.id), evidence='investigated synthetic case')
        key = str(uuid4())
        ack = await returns.acknowledge(session, user, body, key)
        replay = await returns.acknowledge(session, user, body, key)
        assert ack == replay
        state = await returns.reconciliation(session, user)
        assert len(state.buckets['refund_pending']) == 1 and state.buckets['refund_pending'][0].acknowledgements == [ack]
        report = await returns.report(session, user, fiscal.REGISTER, MockReportCreate(kind='x', expected_shift_revision=1, confirm=True), str(uuid4()))
        assert report.totals.sales_minor == 30000 and report.totals.return_count == 0
    async def close():
        async with database() as session:
            try:
                return await returns.report(session, await actor(session), fiscal.REGISTER,
                    MockReportCreate(kind='z', expected_shift_revision=1, confirm=True), str(uuid4()))
            except HTTPException as error:
                assert error.detail['code'] in ('shift_closed', 'shift_revision_conflict')
    result = await asyncio.gather(close(), close())
    assert sum(r is not None for r in result) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockFiscalReport)) == 2
        assert (await fiscal.cash_register(session)).shift_status == 'closed'
        assert not (await returns.reconciliation(session, await actor(session))).buckets['open_shifts']


async def test_share_scope_generation_revocation_expiry_and_safe_public_api(database, checkout_dependencies):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.fiscal_returns import public_router
    from app.core.database import get_db_session
    order, sale = await issued_sale(database, item_name='<script>alert(1)</script>')
    async def share():
        async with database() as session:
            return await returns.share_document(session, await actor(session), sale.id, FiscalShareCreate(), str(uuid4()))
    first = await share()
    token = first.url.rsplit('/', 1)[1]
    application = FastAPI()
    application.include_router(public_router)
    async def sessions():
        async with database() as session:
            yield session
    application.dependency_overrides[get_db_session] = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://synthetic.test') as client:
        response = await client.get('/mock-fiscal-documents/' + token)
        assert response.status_code == 200
        assert set(response.json()) == {'kind', 'items', 'amount_minor', 'currency', 'notice', 'simulation', 'is_sent_dps'}
        assert 'no-store' in response.headers['Cache-Control']
        printed = await client.get('/mock-fiscal-documents/' + token + '/print')
        assert '&lt;script&gt;' in printed.text and '<script>' not in printed.text
        assert (await client.get('/mock-fiscal-documents/' + sale.provider_uuid)).status_code == 404
        assert (await client.post('/mock-fiscal-documents/' + token)).status_code == 405
        second = await share()
        assert second.generation == 2 and (await client.get('/mock-fiscal-documents/' + token)).status_code == 404
        second_token = second.url.rsplit('/', 1)[1]
        async with database() as session:
            await returns.share_document(session, await actor(session), sale.id, FiscalShareRevoke(), str(uuid4()), revoke=True)
        assert (await client.get('/mock-fiscal-documents/' + second_token)).status_code == 404
        third = await share()
        assert third.generation == 3
        assert (await client.get('/mock-fiscal-documents/' + second_token)).status_code == 404
        async with database() as session:
            await session.execute(update(MockFiscalShare).where(MockFiscalShare.generation == 3).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
            await session.commit()
        assert (await client.get('/mock-fiscal-documents/' + third.url.rsplit('/', 1)[1])).status_code == 404


async def test_bigint_full_return_and_exact_server_refund(database, checkout_dependencies):
    order, sale = await issued_sale(database, quantity=220000, completed=False)
    assert sale.snapshot['amount_minor'] == 2_200_000_000
    row = await create(database, order, sale, quantity=220000)
    assert row.amount_minor == 2_200_000_000
    result = await refund(database, row)
    assert result.refund.amount_minor == 2_200_000_000


async def test_z_refuses_unknown_until_same_uuid_reconciliation(database, checkout_dependencies):
    order, sale = await issued_sale(database)
    row = await create(database, order, sale)
    await refund(database, row)
    doc = await submit(database, order.id, approval(await return_preview(database, row), mock_outcome='timeout_after_acceptance'))
    unknown = await command(database, doc, 'process')
    assert unknown.status == 'unknown'
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await returns.report(session, await actor(session), fiscal.REGISTER,
                MockReportCreate(kind='z', expected_shift_revision=1, confirm=True), str(uuid4()))
        assert error.value.detail['code'] == 'shift_unsettled_documents'
    issued = await command(database, unknown, 'process')
    async with database() as session:
        result = await returns.report(session, await actor(session), fiscal.REGISTER,
            MockReportCreate(kind='z', expected_shift_revision=1, confirm=True), str(uuid4()))
        assert result.totals.document_ids == [sale.id, issued.id]
        assert result.totals.sales_minor == 30000 and result.totals.returns_minor == 10000 and result.totals.net_minor == 20000


async def test_synthetic_cod_lineage_carrier_return_never_automates_money_or_stock(database, checkout_dependencies, monkeypatch):
    from app.services import shipments
    from app.schemas.shipment import ShipmentCreate, ShipmentCommand
    from tests.test_shipments_integration import ADDRESS, PACK
    monkeypatch.setattr(settings, 'np_mock_shipments_enabled', True)
    product = await add_product(database, stock=3)
    async with database() as session:
        user = await actor(session)
        prepared = await local_sales.prepare(session, user, POSOrderCreate(items=[{'product_id': product.id, 'quantity': 1}], simulation=True), str(uuid4()))
        order = await session.get(Order, prepared.id)
        # Explicit synthetic fixture, created simulated from its first fact.
        order.fulfillment_method = 'nova_poshta'
        order.shipping_payload_json = ADDRESS.copy()
        await session.commit()
        shipped = await shipments.create(session, user, order.id, ShipmentCreate(expected_revision=prepared.revision, packaging=PACK), str(uuid4()))
        shipped = await shipments.command(session, user, order.id, shipped.shipments[0].id,
            ShipmentCommand(expected_revision=shipped.revision, evidence='synthetic mock COD handover'), str(uuid4()), 'handover')
        shipped = await shipments.command(session, user, order.id, shipped.shipments[0].id,
            ShipmentCommand(expected_revision=shipped.revision, mock_status='returned'), str(uuid4()), 'refresh')
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        assert await session.scalar(select(func.count()).select_from(MockRefund)) == 0
        assert (await session.get(Product, product.id)).stock_quantity == 2
        # There is no NovaPay adapter. A test-only immutable payment fixture models
        # explicit confirmed mock collection, never a carrier-status side effect.
        session.add(OrderPayment(order_id=order.id, amount_minor=10000, method='card', source='operator_confirmation',
            evidence='EXPLICIT SYNTHETIC MOCK COD COLLECTION FIXTURE; no payment processed', operator_id=user.id,
            simulation=True, created_at=datetime.now(UTC)))
        await session.commit()
        await fiscal.shift(session, user, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
    sale = await command(database, await submit(database, order.id, approval(await preview(database, order.id))), 'process')
    row = await create(database, order, sale)
    await refund(database, row)
    from app.services import mock_cod
    from app.schemas.mock_cod import MockParcelReceiptCreate
    async with database() as session:
        current = await session.get(Order, order.id)
        await mock_cod.receive_return(session, user, order.id, shipped.shipments[0].id,
            MockParcelReceiptCreate(expected_revision=current.revision, confirm=True, evidence='Synthetic refused parcel received',
                items=[{'order_item_id': order.items[0].id, 'quantity': 1, 'disposition': 'restock'}]), str(uuid4()))
    acknowledged = await receive(database, row)
    assert acknowledged.receipt.shipment_return_receipt_id is not None
    async with database() as session:
        assert (await session.get(Product, product.id)).stock_quantity == 3
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(MockRefund)) == 1


async def test_zero_minor_partials_and_worker_z_race(database, checkout_dependencies):
    order, sale = await issued_sale(database, additional_discount=29999)
    first = await create(database, order, sale)
    second = await create(database, order, sale)
    third = await create(database, order, sale)
    assert [first.amount_minor, second.amount_minor, third.amount_minor] == [0, 0, 1]
    assert (await refund(database, first)).refund.amount_minor == 0
    doc = await submit(database, order.id, approval(await return_preview(database, first)))
    async def process():
        async with database() as session:
            return await fiscal.process_job(session, await actor(session), doc.id)
    async def close():
        async with database() as session:
            try:
                return await returns.report(session, await actor(session), fiscal.REGISTER,
                    MockReportCreate(kind='z', expected_shift_revision=1, confirm=True), str(uuid4()))
            except HTTPException as error:
                assert error.detail['code'] == 'shift_unsettled_documents'
    issued, report = await asyncio.gather(process(), close())
    assert issued.status == 'issued'
    if report is None:
        async with database() as session:
            report = await returns.report(session, await actor(session), fiscal.REGISTER,
                MockReportCreate(kind='z', expected_shift_revision=1, confirm=True), str(uuid4()))
    assert report.totals.document_ids == [sale.id, doc.id]
    assert report.totals.sales_minor == 1 and report.totals.returns_minor == 0


async def test_backoffice_routes_auth_validation_and_kill_switch(database, checkout_dependencies, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.fiscal_returns import backoffice_router
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    order, sale = await issued_sale(database)
    row = await create(database, order, sale)
    application = FastAPI()
    application.include_router(backoffice_router)
    async def sessions():
        async with database() as session:
            yield session
    async def user():
        async with database() as session:
            return await actor(session)
    application.dependency_overrides[get_db_session] = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://synthetic.test') as client:
        for path in (f'/orders/{order.id}/returns', '/integrations/checkbox/reconciliation',
                f'/integrations/checkbox/cash-registers/{fiscal.REGISTER}/reports'):
            assert (await client.get(path)).status_code == 401
        assert (await client.post(f'/returns/{row.id}/refunds', json={'evidence': 'test'}, headers={'Idempotency-Key': str(uuid4())})).status_code == 401
        application.dependency_overrides[get_current_admin_user] = user
        assert (await client.post(f'/returns/{row.id}/refunds', json={'evidence': 'test'})).status_code == 422
        assert (await client.post(f'/returns/{row.id}/refunds', json={'evidence': 'test', 'amount_minor': 1}, headers={'Idempotency-Key': str(uuid4())})).status_code == 422
        monkeypatch.setattr(settings, 'checkbox_mock_enabled', False)
        listed = await client.get(f'/orders/{order.id}/returns')
        assert listed.status_code == 200 and listed.json()[0]['allowed_actions'] == []
        for path, payload in ((f'/returns/{row.id}/refunds', {'evidence': 'test'}),
                (f'/fiscal-documents/{sale.id}/share', {}), (f'/fiscal-documents/{sale.id}/revoke-share', {}),
                (f'/integrations/checkbox/cash-registers/{fiscal.REGISTER}/reports', {'kind': 'x', 'expected_shift_revision': 1, 'confirm': True})):
            result = await client.post(path, json=payload, headers={'Idempotency-Key': str(uuid4())})
            assert result.status_code == 409 and result.json()['detail']['code'] == 'mock_fiscal_disabled'
