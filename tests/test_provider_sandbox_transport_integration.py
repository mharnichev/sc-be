"""Actual HTTP routes + provider adapters + Alembic PostgreSQL, synthetic I/O only."""
import json
import asyncio
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select

from app.api.v1.routes import fiscal as fiscal_routes, fiscal_returns, orders, qr_payment
from app.core.database import get_db_session
from app.core.security import create_access_token
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.fiscal import FiscalDocument, FiscalJob
from app.models.order import OrderHandover, OrderPayment
from app.models.product import Product
from app.services import sandbox_providers
from tests.test_provider_sandbox_integration import database, flags
from tests.test_inventory_integration import anyio_backend, checkout_dependencies, add_product
from tests.test_sandbox_providers import Provider, INVOICE

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize('actor_kind', ['admin', 'master'])
@pytest.mark.parametrize('receipt_reply', ['pending', 'timeout_after_acceptance', 'invoice_status_timeout'])
async def test_authenticated_routes_real_adapters_and_closed_shift_receipt_recovery(
        database, checkout_dependencies, monkeypatch, actor_kind, receipt_reply):
    product = await add_product(database, stock=5)
    remote = Provider()
    remote.receipt.update(status='CREATED', fiscal_date=None)
    submitted = []
    invoice_reads = 0

    def provider_http(request):
        nonlocal invoice_reads
        path = request.url.path
        if path == '/api/merchant/invoice/status':
            invoice_reads += 1
            if receipt_reply == 'invoice_status_timeout' and invoice_reads == 1:
                remote.calls.append(request)
                raise httpx.ReadTimeout('Synthetic lost bank status response', request=request)
        if path == '/api/merchant/invoice/create':
            body = json.loads(request.content)
            remote.invoice.update(amount=body['amount'], finalAmount=body['amount'],
                                  reference=body['merchantPaymInfo']['reference'])
        if path == '/api/v1/receipts/sell':
            body = json.loads(request.content)
            submitted.append(body)
            remote.receipt.update(id=body['id'], total_sum=body['payments'][0]['value'],
                                  total_payment=body['payments'][0]['value'])
            if receipt_reply == 'timeout_after_acceptance':
                raise httpx.ReadTimeout('Synthetic lost receipt response', request=request)
        if path.startswith('/api/v1/receipts/') and path != '/api/v1/receipts/sell':
            remote.calls.append(request)
            assert path.endswith(remote.receipt['id'])
            return httpx.Response(200, json=remote.receipt, request=request)
        return remote(request)

    adapter = sandbox_providers.SandboxProviders(
        remote.adapter().config, transport=httpx.MockTransport(provider_http))
    monkeypatch.setattr(sandbox_providers, 'get_adapter', lambda: adapter)
    api = FastAPI()
    api.include_router(orders.backoffice_router, prefix='/orders')
    for router in (qr_payment.backoffice_router, fiscal_routes.backoffice_router,
                   fiscal_returns.backoffice_router):
        api.include_router(router)

    async def isolated_session():
        async with database() as session:
            yield session

    api.dependency_overrides[get_db_session] = isolated_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url='http://test') as client:
        denied = await client.get('/integrations/provider-sandbox/readiness')
        assert denied.status_code in (401, 403) and not remote.calls
        client.headers['Authorization'] = 'Bearer ' + create_access_token('1')

        async def post(path, body, key=None, expected=200):
            response = await client.post(path, json=body,
                                        headers={'Idempotency-Key': key or str(uuid4())})
            assert response.status_code == expected, response.text
            return response.json()

        await post('/integrations/checkbox/cash-registers/MOCK-KASA-1/open-shift',
                   {'expected_shift_revision': 0})
        if actor_kind == 'master':
            async with database() as session:
                session.add(AdminUser(id=2, email='minimal-master@example.test', hashed_password='unused',
                    is_active=True, is_superuser=False, can_record_sales=True, can_accept_qr_payments=True))
                session.add(Master(admin_user_id=2, full_name='Isolated minimal master'))
                await session.commit()
            client.headers['Authorization'] = 'Bearer ' + create_access_token('2')
        order = await post('/orders', {'items': [{'product_id': product.id, 'quantity': 2}],
                                       'simulation': True}, expected=201)
        creation = {'expected_revision': order['revision'], 'confirm': True,
                    'cash_register_id': 'MOCK-KASA-1'}
        key = str(uuid4())
        path = f"/orders/{order['id']}/sandbox-qr-payment-attempts"
        creations = await asyncio.gather(*[post(path, creation, key, expected=201) for _ in range(2)])
        assert creations[0]['id'] == creations[1]['id']
        attempt = (await client.get(f"/qr-payment-attempts/{creations[0]['id']}")).json()
        assert attempt['mode'] == 'provider_sandbox'
        assert attempt['payment_url'] == 'https://pay.mbnk.biz/' + INVOICE
        assert not attempt['local_payment_path'] and 'simulate' not in attempt['allowed_actions']
        refresh_path = f"/qr-payment-attempts/{attempt['id']}/sandbox-refresh"
        pending = await post(refresh_path, {'expected_revision': attempt['revision']})
        if receipt_reply == 'invoice_status_timeout':
            assert pending['payment_status'] == 'unknown' and pending['payment_id'] is None
            assert len(submitted) == 0
            pending = await post(refresh_path, {'expected_revision': pending['revision']})
        assert pending['payment_status'] == 'paid' and pending['receipt_status'] == 'pending'
        assert pending['fiscal_document_id'] is None
        read = (await client.get(f"/orders/{order['id']}"))
        assert read.status_code == 200
        assert read.json()['sales_actions']['can_handover'] is False
        await post(f"/orders/{order['id']}/handover",
                   {'expected_revision': read.json()['revision'], 'evidence': 'synthetic'}, expected=409)

        if actor_kind == 'master':
            async with database() as session:
                master = await session.get(AdminUser, 2)
                master.can_accept_qr_payments = False
                await session.commit()
            calls_before = len(remote.calls)
            await post(refresh_path, {'expected_revision': pending['revision']}, expected=403)
            await post(path, creation, key=str(uuid4()), expected=403)
            assert (await client.get(f"/qr-payment-attempts/{attempt['id']}/receipt")).status_code == 403
            await post(f"/orders/{order['id']}/handover",
                       {'expected_revision': read.json()['revision'], 'evidence': 'revoked'}, expected=403)
            assert len(remote.calls) == calls_before
            async with database() as session:
                master = await session.get(AdminUser, 2)
                master.can_accept_qr_payments = True
                await session.commit()
        # No provider-client memory survives the pending result; only durable IDs do.
        monkeypatch.setattr(sandbox_providers, 'get_adapter', lambda: sandbox_providers.SandboxProviders(
            remote.adapter().config, transport=httpx.MockTransport(provider_http)))

        # The already accepted receipt is recoverable after the original shift
        # closes. No new receipt or shift may be submitted during that recovery.
        remote.shift['status'] = 'CLOSED'
        remote.receipt.update(status='DONE', fiscal_date='2026-10-18T15:01:00+03:00')
        issued = await post(refresh_path, {'expected_revision': pending['revision']})
        assert issued['receipt_status'] == 'issued'
        receipt = await client.get(f"/qr-payment-attempts/{attempt['id']}/receipt")
        assert receipt.status_code == 200 and 'Checkbox TEST' in receipt.text
        assert remote.receipt['id'] in receipt.text and INVOICE in receipt.text
        current = (await client.get(f"/orders/{order['id']}"))
        assert current.status_code == 200 and current.json()['sales_actions']['can_handover']
        handover = {'expected_revision': current.json()['revision'], 'evidence': 'synthetic handover'}
        key = str(uuid4())
        handed_over = await asyncio.gather(*[
            post(f"/orders/{order['id']}/handover", handover, key) for _ in range(2)])
        assert handed_over[0]['id'] == handed_over[1]['id'] == order['id']
        await post(f"/orders/{order['id']}/handover", handover, expected=409)
        client.headers['Authorization'] = 'Bearer ' + create_access_token('1')
        for kind in ('x', 'z'):
            report = await post('/integrations/checkbox/cash-registers/MOCK-KASA-1/reports',
                                {'kind': kind, 'expected_shift_revision': 1, 'confirm': True})
            assert report['totals']['sale_count'] == 1
            assert report['totals']['sales_minor'] == report['totals']['net_minor'] == 20000

    assert len(submitted) == 1 and 'delivery' not in submitted[0]
    assert len([r for r in remote.calls if r.url.path == '/api/merchant/invoice/create']) == 1
    assert not any(r.url.path == '/api/v1/shifts' and r.method == 'POST' for r in remote.calls)
    async with database() as session:
        item = await session.get(Product, product.id)
        assert (item.stock_quantity, item.reserved_quantity) == (3, 0)
        for model in (OrderPayment, OrderHandover, FiscalDocument):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalJob)) == 0
