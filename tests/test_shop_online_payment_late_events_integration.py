"""Late provider observations cannot authorize another financial attempt.

Disposable guarded localhost PostgreSQL fixtures and MockTransport only. These
are polling-order regressions, not proof of irreversible bank closure or a claim
that a webhook/remove API exists.
"""
import asyncio

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.models.order import OrderPayment
from app.models.product import Product
from app.models.shop_online_payment import ShopOnlinePaymentRequest
from app.schemas.shop_online_payment import OnlinePaymentMethodCommand
from app.services import shop_online_payment as service, mono_checkbox_poll as poll, shop_payments as payments
from app.services.monobank_provider import MonobankGateway
from tests.test_shop_online_payment_integration import online, prepared, start  # noqa: F401
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_online_payment_actions_integration import change

pytestmark = pytest.mark.anyio


@pytest.fixture(scope='module')
def anyio_backend():
    return 'asyncio'


async def read(database, identifier, token):
    async with database() as session:
        return await service.read(session, identifier, None, token)


async def rewind_poll(database):
    async with database() as session:
        await session.execute(text("UPDATE mono_checkbox_poll_state SET last_attempt_at=now()-interval '2 hours'"))
        await session.commit()


async def assert_no_new_attempt(database, identifier, token, online):
    state = await read(database, identifier, token)
    assert not state.actions.start and not state.actions.retry and not state.actions.change_method
    assert state.available_payment_methods == []
    calls_before = list(online['calls'])
    again = await start(database, identifier, token)
    assert again.attempt_id == state.attempt_id
    command = OnlinePaymentMethodCommand(expected_revision=again.revision,
        expected_attempt_id=again.attempt_id, payment_method='cash')
    with pytest.raises(HTTPException) as blocked:
        await change(database, identifier, token, command)
    assert blocked.value.status_code == 409
    assert online['calls'] == calls_before and online['posts'] == 1
    return again


@pytest.mark.parametrize('initial_status', ['failure','expired','hold','processing','reversed'])
async def test_late_success_same_invoice_concurrent_owner_actions_remains_single_payment(
        database, checkout_dependencies, online, initial_status):
    identifier, product, token = await prepared(database)
    initial = await start(database, identifier, token)
    async with database() as session:
        order = await service.sales.load_order(session, identifier)
        original_invoice = order.shop_payment_intent.invoice_id
        request_id = order.shop_online_payment_request.id
    online['status'] = initial_status
    await poll.poll_once(database)
    before = await assert_no_new_attempt(database, identifier, token, online)
    assert not before.paid
    online['status'] = 'success'
    await rewind_poll(database)
    payload = OnlinePaymentMethodCommand(expected_revision=before.revision,
        expected_attempt_id=before.attempt_id, payment_method='cash')
    results = await asyncio.gather(start(database, identifier, token), read(database, identifier, token),
        change(database, identifier, token, payload), poll.poll_once(database), return_exceptions=True)
    assert isinstance(results[2], HTTPException) and results[2].status_code == 409
    for index in [0,1,3]:
        assert not isinstance(results[index], Exception), results[index]
    # Duplicate reconciliation must not record the same money again.
    await rewind_poll(database)
    await poll.poll_once(database)
    final = await assert_no_new_attempt(database, identifier, token, online)
    assert final.paid and final.status in {'paid_receipt_pending','accepted'}
    assert final.attempt_id == initial.attempt_id == request_id
    async with database() as session:
        order = await service.sales.load_order(session, identifier)
        assert order.shop_payment_intent.invoice_id == original_invoice
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        stock = await session.get(Product, product)
        assert stock.stock_quantity == 4 and stock.reserved_quantity == 1
    assert online['posts'] == 1


@pytest.mark.parametrize('stale_status', ['processing','failure'])
async def test_paid_money_survives_later_stale_provider_observation(database, checkout_dependencies, online, stale_status):
    identifier, _, token = await prepared(database)
    await start(database, identifier, token)
    online['status'] = 'success'
    await poll.poll_once(database)
    assert (await read(database, identifier, token)).paid
    online['status'] = stale_status
    await rewind_poll(database)
    await poll.poll_once(database)
    state = await assert_no_new_attempt(database, identifier, token, online)
    assert state.paid
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest)) == 1


async def test_unknown_create_timeout_not_reopened_when_transport_recovers(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    online['unknown'] = True
    initial = await start(database, identifier, token)
    assert initial.status == 'creation_unknown'
    online['unknown'] = False
    online['status'] = 'success'
    # Without the acknowledged invoice identity, ordinary poll cannot attach a
    # guessed invoice. The separately authorized exact-identity recovery owns it.
    await poll.poll_once(database)
    final = await assert_no_new_attempt(database, identifier, token, online)
    assert final.status == 'creation_unknown' and not final.paid
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest)) == 1


@pytest.mark.parametrize('failure', ['timeout','not_found'])
async def test_provider_get_error_is_not_irreversible_closure(database, checkout_dependencies, online, monkeypatch, failure):
    identifier, _, token = await prepared(database)
    await start(database, identifier, token)
    original_handler = online['transport'].handler
    def handler(request):
        if request.method == 'GET' and request.url.path.endswith('/invoice/status'):
            online['calls'].append((request.method, request.url.path))
            if failure == 'timeout':
                raise httpx.ReadTimeout('synthetic status acknowledgement unavailable', request=request)
            return httpx.Response(404, json={'errCode':'NOT_FOUND'})
        return original_handler(request)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(payments, 'gateway', lambda: MonobankGateway(transport=transport))
    await poll.poll_once(database)
    state = await assert_no_new_attempt(database, identifier, token, online)
    assert not state.paid
    assert any(method=='GET' and path.endswith('/invoice/status') for method,path in online['calls'])
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest)) == 1
