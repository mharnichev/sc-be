"""Owner payment actions; migrated disposable localhost PG, mocked provider only.

Run with INVENTORY_TEST_DATABASE_URL pointing to a disposable localhost *test DB.
Reuses the guarded template/isolated per-test database from fiscal integration.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select

from app.api.v1.routes.shop_online_payment import public_router
from app.core.database import get_db_session
from app.core.security import create_scoped_token
from app.dependencies.auth import get_optional_current_customer
from app.models.customer import Customer
from app.models.order import Order
from app.models.product import Product
from app.models.shop_online_payment import ShopOnlinePaymentRequest
from app.schemas.shop_online_payment import OnlinePaymentMethodCommand
from app.services import shop_online_payment as service, mono_checkbox_poll as poll
from tests.test_shop_online_payment_integration import online, prepared, start  # noqa: F401
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_sales_integration import seed, attach

pytestmark = pytest.mark.anyio


@pytest.fixture(scope='module')
def anyio_backend():
    return 'asyncio'


async def change(database, identifier, token, payload, key=None, customer=None):
    async with database() as session:
        return await service.change_method(session, identifier, customer, token, payload, key or str(uuid4()))


async def command(database, identifier, method='cash'):
    async with database() as session:
        state = await service.read(session, identifier, None, (await session.get(Order, identifier)).guest_payment_token)
    return OnlinePaymentMethodCommand(expected_revision=state.revision, expected_attempt_id=state.attempt_id, payment_method=method)


def app_for(database, customer=None):
    app = FastAPI()
    app.include_router(public_router)
    async def db():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_optional_current_customer] = lambda: customer
    return app


@pytest.mark.parametrize('kind,target', [('pickup','cash'), ('nova_poshta','cashOnDelivery')])
async def test_pre_dispatch_method_change_replay_stale_and_stock_unchanged(database, checkout_dependencies, online, kind, target):
    identifier, product = await seed(database, kind=kind, method='onlineCard', quantity=2)
    async with database() as session:
        order = await session.get(Order, identifier)
        token = order.guest_payment_token
        before = await service.read(session, identifier, None, token)
        stock = await session.get(Product, product)
        stock_before = (stock.stock_quantity, stock.reserved_quantity)
    assert before.actions.start and before.actions.change_method and not before.actions.retry
    assert before.available_payment_methods == [target]
    assert before.order.model_dump() == dict(id=identifier, number=str(identifier), status='pending', fulfillment_method=kind, item_count=2, payment_method='onlineCard')
    payload = OnlinePaymentMethodCommand(expected_revision=before.revision, expected_attempt_id=None, payment_method=target)
    key = str(uuid4())
    result = await change(database, identifier, token, payload, key)
    assert result.revision == before.revision + 1
    assert result.order.payment_method == target and result.attempt_id is None
    assert result.reason == 'offline_payment_selected'
    assert not result.actions.start and not result.actions.retry and not result.actions.change_method
    replay = await change(database, identifier, token, payload, key)
    assert replay.model_dump() == result.model_dump()
    with pytest.raises(HTTPException) as stale:
        await change(database, identifier, token, payload)
    assert stale.value.status_code == 409
    async with database() as session:
        stock = await session.get(Product, product)
        assert (stock.stock_quantity, stock.reserved_quantity) == stock_before
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest)) == 0
    assert online['calls'] == []


@pytest.mark.parametrize('invalid', ['wrong_method', 'attempt', 'reservation_ownership', 'shipment', 'zero_amount'])
async def test_change_rejects_ineligible_facts_without_provider_io(database, checkout_dependencies, online, invalid):
    identifier, product, token = await prepared(database)
    payload = await command(database, identifier)
    if invalid == 'wrong_method':
        payload.payment_method = 'cashOnDelivery'
    elif invalid == 'attempt':
        payload.expected_attempt_id = 123
    elif invalid == 'shipment':
        await attach(database, identifier)
    else:
        async with database() as session:
            if invalid == 'reservation_ownership':
                # Leave the immutable owned reservation ledger intact while the
                # order item no longer claims the reserved stock quantity.
                order = await service.sales.load_order(session, identifier)
                order.items[0].quantity_from_stock = 0
                order.items[0].quantity_to_order = order.items[0].quantity
            else:
                (await session.get(Order, identifier)).total_amount = 0
            await session.commit()
    with pytest.raises(HTTPException) as denied:
        await change(database, identifier, token, payload)
    assert denied.value.status_code == 409
    async with database() as session:
        assert (await session.get(Order, identifier)).payment_method == 'onlineCard'
    assert online['calls'] == []


@pytest.mark.parametrize('provider_status', ['failure','expired','reversed','success'])
async def test_dispatched_terminal_states_never_authorize_retry_or_change(database, checkout_dependencies, online, provider_status):
    identifier, _, token = await prepared(database)
    await start(database, identifier, token)
    online['status'] = provider_status
    await poll.poll_once(database)
    calls_before = list(online['calls'])
    async with database() as session:
        state = await service.read(session, identifier, None, token)
    assert state.attempt_id is not None
    assert not state.actions.start and not state.actions.retry and not state.actions.change_method
    assert state.available_payment_methods == []
    assert state.status == ('paid_receipt_pending' if provider_status == 'success' else 'requires_review')
    assert state.reason == {'failure':'payment_failed', 'expired':'invoice_expired',
        'reversed':'payment_reversed', 'success':'payment_verified_receipt_pending'}[provider_status]
    payload = OnlinePaymentMethodCommand(expected_revision=state.revision, expected_attempt_id=state.attempt_id, payment_method='cash')
    with pytest.raises(HTTPException) as denied:
        await change(database, identifier, token, payload)
    assert denied.value.status_code == 409
    assert online['calls'] == calls_before and online['posts'] == 1


@pytest.mark.parametrize('unknown', [False, True])
async def test_waiting_or_unknown_attempt_stays_one_shot(database, checkout_dependencies, online, unknown):
    identifier, _, token = await prepared(database)
    online['unknown'] = unknown
    state = await start(database, identifier, token)
    assert state.status == ('creation_unknown' if unknown else 'waiting')
    assert state.reason == ('invoice_creation_unknown' if unknown else 'payment_pending')
    assert not state.actions.start and not state.actions.retry and not state.actions.change_method
    assert state.actions.continue_payment == (not unknown)
    calls_before = list(online['calls'])
    payload = OnlinePaymentMethodCommand(expected_revision=state.revision, expected_attempt_id=state.attempt_id, payment_method='cash')
    with pytest.raises(HTTPException):
        await change(database, identifier, token, payload)
    assert online['calls'] == calls_before
    await start(database, identifier, token)
    assert online['posts'] == 1


async def test_concurrent_start_and_change_serialize_without_double_dispatch(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    payload = await command(database, identifier)
    results = await asyncio.gather(start(database, identifier, token), change(database, identifier, token, payload), return_exceptions=True)
    assert any(not isinstance(r, Exception) for r in results)
    assert online['posts'] <= 1
    async with database() as session:
        order = await session.get(Order, identifier)
        count = await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest))
        if order.payment_method == 'cash':
            assert count == 0 and online['posts'] == 0
        else:
            assert order.payment_method == 'onlineCard' and count == 1 and online['posts'] == 1


@pytest.mark.parametrize('access', ['missing','wrong','wrong_scope','wrong_order','expired_jwt','expired_order','revoked','wrong_customer','invalid_auth'])
async def test_change_http_owner_guards_and_private_errors(database, checkout_dependencies, online, access):
    identifier, _, token = await prepared(database)
    payload = (await command(database, identifier)).model_dump()
    headers = {'Idempotency-Key':str(uuid4()), 'X-Order-Payment-Token':token}
    customer = None
    if access == 'missing': headers.pop('X-Order-Payment-Token')
    if access == 'wrong': headers['X-Order-Payment-Token'] = 'invalid'
    if access in {'wrong_scope','wrong_order','expired_jwt'}:
        headers['X-Order-Payment-Token'] = create_scoped_token(identifier+1 if access=='wrong_order' else identifier,
            'guest_order_tracking' if access=='wrong_scope' else 'guest_order_payment',
            datetime.now(UTC)+timedelta(seconds=-1 if access=='expired_jwt' else 3600))
    if access in {'expired_order','revoked'}:
        async with database() as session:
            order = await session.get(Order, identifier)
            if access == 'expired_order': order.guest_tracking_expires_at = datetime.now(UTC)-timedelta(seconds=1)
            else: order.guest_tracking_revoked_at = datetime.now(UTC)
            await session.commit()
    if access == 'wrong_customer': customer = SimpleNamespace(id=999)
    if access == 'invalid_auth': headers['Authorization'] = 'Bearer invalid'
    app = app_for(database, customer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/orders/{identifier}/online-payment/change-method', json=payload, headers=headers)
    assert response.status_code == (401 if access=='invalid_auth' else 404)
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert online['calls'] == []


async def test_customer_owner_change_and_guest_cannot_reuse_token(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    payload = await command(database, identifier)
    async with database() as session:
        owner = Customer(phone='+380500009981', name='Private name')
        session.add(owner)
        await session.flush()
        (await session.get(Order, identifier)).customer_id = owner.id
        await session.commit()
        owner_id = owner.id
    with pytest.raises(HTTPException) as denied:
        await change(database, identifier, token, payload)
    assert denied.value.status_code == 404
    state = await change(database, identifier, None, payload, customer=SimpleNamespace(id=owner_id))
    assert state.order.payment_method == 'cash'
    public_order = state.model_dump()['order']
    assert set(public_order) == {'id','number','status','fulfillment_method','item_count','payment_method'}
    assert 'Private name' not in str(state.model_dump()) and '+380' not in str(state.model_dump())
    assert online['calls'] == []


@pytest.mark.parametrize('defect', ['extra','missing_key','missing_attempt','unsupported_method'])
async def test_change_http_validation_never_dispatches_and_is_no_store(database, checkout_dependencies, online, defect):
    identifier, _, token = await prepared(database)
    payload = (await command(database, identifier)).model_dump()
    headers = {'Idempotency-Key':str(uuid4()), 'X-Order-Payment-Token':token}
    if defect == 'extra': payload['phone'] = 'must-not-be-accepted'
    elif defect == 'missing_key': headers.pop('Idempotency-Key')
    elif defect == 'missing_attempt': payload.pop('expected_attempt_id')
    else: payload['payment_method'] = 'onlineCard'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(database)), base_url='http://test') as client:
        response = await client.post(f'/orders/{identifier}/online-payment/change-method',json=payload,headers=headers)
    assert response.status_code == 422 and response.headers['cache-control'] == 'no-store'
    assert online['calls'] == []


async def test_accepted_receipt_actions_cannot_reopen_payment(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    await start(database, identifier, token)
    online['status'] = 'success'
    online['checks'] = [dict(id=str(uuid4()), status='done', type='sale', fiscalizationSource='checkbox')]
    assert await poll.poll_once(database) == {'checked':1, 'blocked':0}
    calls_before = list(online['calls'])
    async with database() as session:
        state = await service.read(session, identifier, None, token)
    assert state.status == 'accepted' and state.paid and state.receipt_accepted
    assert not state.actions.start and not state.actions.continue_payment
    assert not state.actions.retry and not state.actions.change_method
    assert state.available_payment_methods == []
    assert online['calls'] == calls_before


async def test_http_method_change_success_and_conflict_are_private(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    payload = (await command(database, identifier)).model_dump()
    headers = {'Idempotency-Key':str(uuid4()), 'X-Order-Payment-Token':token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(database)), base_url='http://test') as client:
        response = await client.post(f'/orders/{identifier}/online-payment/change-method',json=payload,headers=headers)
        assert response.status_code == 200 and response.json()['order']['payment_method'] == 'cash'
        assert response.headers['cache-control'] == 'no-store' and response.headers['pragma'] == 'no-cache'
        headers['Idempotency-Key'] = str(uuid4())
        stale = await client.post(f'/orders/{identifier}/online-payment/change-method',json=payload,headers=headers)
        assert stale.status_code == 409 and stale.headers['cache-control'] == 'no-store'
    assert online['calls'] == []


async def test_reused_idempotency_key_rejects_changed_payload(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    payload = await command(database, identifier)
    key = str(uuid4())
    first = await change(database, identifier, token, payload, key)
    malicious = payload.model_copy(update={"payment_method":"cashOnDelivery"})
    with pytest.raises(HTTPException) as conflict:
        await change(database, identifier, token, malicious, key)
    assert conflict.value.status_code == 409
    async with database() as session:
        state = await service.read(session, identifier, None, token)
    assert state.revision == first.revision
    assert state.order.payment_method == 'cash'
    assert state.attempt_id is None and online['calls'] == []


async def test_initial_cod_read_uses_neutral_offline_reason(database, checkout_dependencies, online):
    identifier, _ = await seed(database, kind='nova_poshta', method='cashOnDelivery')
    async with database() as session:
        token = (await session.get(Order, identifier)).guest_payment_token
        state = await service.read(session, identifier, None, token)
    assert state.reason == 'offline_payment_selected'
    assert not state.actions.start and not state.actions.retry and not state.actions.change_method
    assert state.attempt_id is None and online['calls'] == []


@pytest.mark.parametrize('relation', [
    'shop_online_payment_request', 'shop_payment_intent', 'payments', 'qr_payment_attempts',
    'handovers', 'shipments', 'shop_payment_confirmation', 'shop_payment_evidence',
    'shop_fulfillment_fact', 'shop_completion', 'shop_payment_resolutions', 'shop_carrier_returns',
    'shop_np_returns', 'shop_fiscal_documents', 'shop_financial_returns', 'shop_checkbox_binding',
    'shop_checkbox_documents', 'shop_checkbox_returns', 'shop_mono_checkbox_receipt',
    'shop_mono_checkbox_observations', 'master_mono_create_request', 'master_mono_binding',
    'master_mono_payment', 'master_mono_receipt', 'master_mono_observations'])
async def test_unloaded_or_present_financial_relation_fails_closed(
        database, checkout_dependencies, online, relation):
    identifier, _, _ = await prepared(database)
    async with database() as session:
        order = await service.sales.load_order(session, identifier)
        assert service.pre_dispatch_payment_methods(order) == ['cash']
        session.expire(order, [relation])
        assert service.pre_dispatch_payment_methods(order) == []
        await session.refresh(order, [relation])
        assert service.pre_dispatch_payment_methods(order) == ['cash']
        # Inspect the decision without mutating/persisting a synthetic ledger row.
        order.__dict__[relation] = object()
        assert service.pre_dispatch_payment_methods(order) == []
    assert online['calls'] == []


async def test_existing_shipment_blocks_start_without_provider_io(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    await attach(database, identifier)
    with pytest.raises(HTTPException) as denied:
        await start(database, identifier, token)
    assert denied.value.status_code == 409
    assert online['calls'] == []
