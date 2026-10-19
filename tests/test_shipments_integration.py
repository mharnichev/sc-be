"""Local test PostgreSQL + durable mock carrier only; no provider/network calls."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from app.core.config import settings
from app.core.security import create_scoped_token
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderHandover, OrderPayment, OrderStatus
from app.models.shipment import Shipment, ShipmentEvent
from app.models.product import Product
from app.schemas.order import PreliminaryDocumentCreate, PublicOrderResponse, OrderHandoverCreate
from app.schemas.shipment import ShipmentCreate, ShipmentCommand, ShipmentFixture
from app.services import shipments, local_sales
from app.services.order import OrderService
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product, checkout_payload

pytestmark = pytest.mark.anyio
ADDRESS = {'shippingCityRef': '11111111-1111-4111-8111-111111111111',
           'shippingWarehouseRef': '22222222-2222-4222-8222-222222222222',
           'cityLabel': 'Тестове місто', 'warehouseLabel': 'Тестове відділення'}
PACK = {'weight_kg': .5, 'length_cm': 20, 'width_cm': 15, 'height_cm': 10,
        'places': 1, 'payer': 'recipient', 'declared_value_minor': 10000, 'cod_amount_minor': 10000}


@pytest.fixture(autouse=True)
def enable_mock(monkeypatch):
    monkeypatch.setattr(settings, 'np_mock_shipments_enabled', True)
    monkeypatch.setattr(settings, 'app_env', 'test')


async def setup_order(database, checkout_dependencies, stock=2):
    product = await add_product(database, stock=stock)
    async with database() as session:
        order = await OrderService().create_order(session, checkout_payload(product.id))
        order.fulfillment_method = 'nova_poshta'
        order.shipping_payload_json = ADDRESS.copy()
        await session.commit()
        return order.id, product.id


async def create(database, oid, payload=None, key=None):
    async with database() as session:
        return await shipments.create(session, await session.get(AdminUser, 1), oid,
            payload or ShipmentCreate(expected_revision=1, packaging=PACK), key or str(uuid4()))


async def command(database, oid, sid, revision, op='refresh', key=None, **kwargs):
    async with database() as session:
        return await shipments.command(session, await session.get(AdminUser, 1), oid, sid,
            ShipmentCommand(expected_revision=revision, **kwargs), key or str(uuid4()), op)


async def test_carrier_lifecycle_reserves_until_explicit_handover(database, checkout_dependencies):
    oid, pid = await setup_order(database, checkout_dependencies)
    key = str(uuid4())
    a, b = await asyncio.gather(create(database, oid, key=key), create(database, oid, key=key))
    assert a.model_dump() == b.model_dump()
    sid = a.shipments[0].id
    assert a.shipments[0].simulation and a.payment_status == 'unpaid'
    assert a.shipment_actions['can_handover']
    async with database() as session:
        product = await session.get(Product, pid)
        assert (product.stock_quantity, product.reserved_quantity) == (2, 1)
    with pytest.raises(HTTPException) as error:
        await create(database, oid, ShipmentCreate(expected_revision=2, packaging=PACK))
    assert error.value.detail['code'] == 'active_shipment_exists'
    hand_key = str(uuid4())
    c, d = await asyncio.gather(command(database, oid, sid, 2, 'handover', hand_key, evidence='Mock carrier accepted'),
                              command(database, oid, sid, 2, 'handover', hand_key, evidence='Mock carrier accepted'))
    assert c.model_dump() == d.model_dump()
    assert c.fulfillment_status == 'simulated_carrier_handover' and c.payment_status == 'unpaid'
    polled = await command(database, oid, sid, c.revision)
    assert polled.shipments[0].status == 'handed_over'
    delivered = await command(database, oid, sid, polled.revision, mock_status='delivered')
    stale = await command(database, oid, sid, delivered.revision, mock_status='in_transit')
    assert stale.shipments[0].status == 'delivered' and stale.payment_status == 'unpaid'
    assert stale.shipments[0].events[-1].outcome == 'ignored_terminal'
    with pytest.raises(HTTPException) as error:
        await command(database, oid, sid, stale.revision, 'cancel')
    assert error.value.detail['code'] == 'already_handed_over'
    async with database() as session:
        product = await session.get(Product, pid)
        assert (product.stock_quantity, product.reserved_quantity) == (1, 0)
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        assert await session.scalar(select(func.count()).select_from(Shipment)) == 1
    replay = await create(database, oid, key=key)
    assert replay.model_dump() == a.model_dump()
    with pytest.raises(HTTPException) as error:
        await create(database, oid, ShipmentCreate(expected_revision=1, packaging={**PACK, 'places': 2}), key)
    assert error.value.detail['code'] == 'idempotency_conflict'


async def test_unknown_reconciles_same_uuid_and_cancel_releases_no_stock(database, checkout_dependencies):
    oid, pid = await setup_order(database, checkout_dependencies)
    request = ShipmentCreate(expected_revision=1, packaging=PACK, mock_outcome='timeout_after_acceptance')
    result = await create(database, oid, request)
    ship = result.shipments[0]
    assert ship.status == 'unknown' and ship.tracking_number is None
    with pytest.raises(HTTPException):
        await create(database, oid, ShipmentCreate(expected_revision=2, packaging=PACK))
    async with database() as session:
        polled = await shipments.poll_mock_shipments(session, await session.get(AdminUser, 1))
        assert polled['polled'] == [ship.id]
    reconciled = await command(database, oid, ship.id, 3)
    assert reconciled.shipments[0].operation_uuid == ship.operation_uuid
    assert reconciled.shipments[0].tracking_number.startswith('MOCK-')
    cancelled = await command(database, oid, ship.id, reconciled.revision, 'cancel')
    assert not cancelled.shipments[0].active
    async with database() as session:
        product = await session.get(Product, pid)
        assert (product.stock_quantity, product.reserved_quantity) == (2, 1)
        assert await session.scalar(select(func.count()).select_from(Shipment)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 0


async def test_refusal_return_never_restock_or_pay_and_unknown_status_review(database, checkout_dependencies):
    oid, pid = await setup_order(database, checkout_dependencies)
    a = await create(database, oid)
    sid = a.shipments[0].id
    a = await command(database, oid, sid, a.revision, 'handover', evidence='Mock handover')
    for raw in ['in_transit', 'refused', 'returning', 'returned', 'created']:
        a = await command(database, oid, sid, a.revision, mock_status=raw)
    assert a.shipments[0].status == 'returned'
    async with database() as session:
        product = await session.get(Product, pid)
        assert (product.stock_quantity, product.reserved_quantity) == (1, 0)
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0


async def test_attach_verified_fixture_foreign_forbidden_and_correct_legacy_address(database, checkout_dependencies):
    oid, _ = await setup_order(database, checkout_dependencies)
    async with database() as session:
        order = await session.get(Order, oid)
        order.shipping_payload_json = {'shippingCity': 'Legacy text'}
        order.fulfillment_method = None
        order.shipping_company = 'novaPoshta'
        order.channel = None
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await create(database, oid)
    assert error.value.detail['code'] == 'address_refs_required'
    async with database() as session:
        fixture = await shipments.fixture(session, await session.get(AdminUser, 1), oid,
            ShipmentFixture(expected_revision=1, address_snapshot=ADDRESS), str(uuid4()))
    with pytest.raises(HTTPException) as error:
        await create(database, oid, ShipmentCreate(expected_revision=1, source='attach',
            tracking_number='MOCK-' + uuid4().hex.upper(), address_snapshot=ADDRESS, packaging=PACK))
    assert error.value.detail['code'] == 'mock_tracking_not_verified'
    a = await create(database, oid, ShipmentCreate(expected_revision=1, source='attach',
            tracking_number=fixture['tracking_number'], address_snapshot=ADDRESS, packaging=PACK))
    assert a.shipments[0].source == 'attach'
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await OrderService().update_status(session, oid, OrderStatus.completed, admin_user_id=1)
        assert error.value.detail['code'] == 'profile_command_required'
    assert 'НЕ ДЛЯ ВІДПРАВЛЕННЯ' in shipments.label_html(a.shipments[0])
    a = await command(database, oid, a.shipments[0].id, a.revision, mock_status='unrecognized')
    assert a.shipments[0].status == 'requires_review'
    async with database() as session:
        order = await session.get(Order, oid)
        assert order.shipping_payload_json == {'shippingCity': 'Legacy text'}


async def test_guest_scope_expiry_revocation_and_safe_public_projection(database, checkout_dependencies):
    oid, _ = await setup_order(database, checkout_dependencies)
    a = await create(database, oid)
    async with database() as session:
        order = await local_sales.load(session, oid)
        token = order.guest_tracking_url.rsplit('/', 1)[1]
        safe = await shipments.guest_tracking(session, token)
        assert safe['shipments'][0].id == a.shipments[0].id
        assert 'address_snapshot' not in str(safe) and 'operation_uuid' not in str(safe)
        public = PublicOrderResponse.model_validate(order).model_dump()
        assert 'packaging' not in str(public['shipments'])
        for invalid in ['invalid', create_scoped_token(oid, 'preliminary_document', datetime.now(UTC) + timedelta(days=1)),
                        create_scoped_token(oid, 'guest_order_tracking', datetime.now(UTC) - timedelta(seconds=1))]:
            with pytest.raises(HTTPException) as error:
                await shipments.guest_tracking(session, invalid)
            assert error.value.status_code == 404
        order.guest_tracking_revoked_at = datetime.now(UTC)
        await session.commit()
        with pytest.raises(HTTPException):
            await shipments.guest_tracking(session, token)


async def test_guards_and_no_legacy_handover_bypass(database, checkout_dependencies, monkeypatch):
    oid, _ = await setup_order(database, checkout_dependencies)
    monkeypatch.setattr(settings, 'np_mock_shipments_enabled', False)
    with pytest.raises(HTTPException) as error:
        await create(database, oid)
    assert error.value.detail['code'] == 'mock_shipments_disabled'
    monkeypatch.setattr(settings, 'np_mock_shipments_enabled', True)
    async with database() as session:
        actor = SimpleNamespace(is_superuser=False, can_manage_shipments=False, id=1, can_post_inventory=True)
        with pytest.raises(HTTPException) as error:
            await shipments.create(session, actor, oid, ShipmentCreate(expected_revision=1, packaging=PACK), str(uuid4()))
        assert error.value.status_code == 403
    a = await create(database, oid)
    async with database() as session:
        order = await session.get(Order, oid)
        order.simulation = True
        await session.commit()
    monkeypatch.setattr(settings, 'local_sales_simulation_enabled', True)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await local_sales.command(session, await session.get(AdminUser, 1), oid,
                OrderHandoverCreate(expected_revision=a.revision, evidence='bypass'), str(uuid4()), handover=True)
        assert error.value.detail['code'] == 'carrier_handover_required'
    for op in (OrderStatus.completed, OrderStatus.paid):
        async with database() as session:
            with pytest.raises(HTTPException) as error:
                await OrderService().update_status(session, oid, op, admin_user_id=1)
            assert error.value.detail['code'] == 'profile_command_required'


async def test_different_keys_compete_for_one_active_and_one_handover(database, checkout_dependencies):
    oid, pid = await setup_order(database, checkout_dependencies)
    async def attempt_create():
        try:
            return await create(database, oid)
        except HTTPException as error:
            return error.status_code
    created = await asyncio.gather(attempt_create(), attempt_create())
    assert sum(item == 409 for item in created) == 1
    order = next(item for item in created if item != 409)
    sid = order.shipments[0].id
    async def attempt_handover():
        try:
            return await command(database, oid, sid, order.revision, 'handover', evidence='Concurrent mock handover')
        except HTTPException as error:
            return error.status_code
    handed = await asyncio.gather(attempt_handover(), attempt_handover())
    assert sum(item == 409 for item in handed) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Shipment)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
        product = await session.get(Product, pid)
        assert (product.stock_quantity, product.reserved_quantity) == (1, 0)


async def test_actual_api_auth_foreign_access_headers_label_and_guest(database, checkout_dependencies):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.orders import backoffice_router, public_router
    from app.dependencies.auth import get_current_admin_user, get_current_customer
    from app.core.database import get_db_session
    oid, pid = await setup_order(database, checkout_dependencies)
    application = FastAPI()
    application.include_router(backoffice_router, prefix='/admin/orders')
    application.include_router(public_router, prefix='/public/orders')
    async def sessions():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await session.get(AdminUser, 1)
    application.dependency_overrides[get_db_session] = sessions
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url='http://isolated.test') as client:
        body = {'expected_revision': 1, 'packaging': PACK}
        assert (await client.post(f'/admin/orders/{oid}/shipments', json=body,
                                 headers={'Idempotency-Key': str(uuid4())})).status_code == 401
        application.dependency_overrides[get_current_admin_user] = actor
        assert (await client.post(f'/admin/orders/{oid}/shipments', json=body)).status_code == 422
        assert (await client.post(f'/admin/orders/{oid}/shipments', json=body,
                                 headers={'Idempotency-Key': 'predictable'})).status_code == 422
        a = await client.post(f'/admin/orders/{oid}/shipments', json=body,
                             headers={'Idempotency-Key': str(uuid4())})
        assert a.status_code == 201
        sid = a.json()['shipments'][0]['id']
        reloaded = await client.get(f'/admin/orders/{oid}')
        assert reloaded.status_code == 200
        assert reloaded.json()['shipments'][0]['label_url'] == f'/api/v1/backoffice/orders/{oid}/shipments/{sid}/label'
        label = await client.get(f'/admin/orders/{oid}/shipments/{sid}/label')
        assert label.status_code == 200 and 'НЕ ДЛЯ ВІДПРАВЛЕННЯ' in label.text
        assert label.headers['cache-control'] == 'no-store'
        assert (await client.get(f'/admin/orders/{oid + 1}/shipments/{sid}/label')).status_code == 404
        application.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id=99999)
        assert (await client.get(f'/public/orders/me/{oid}')).status_code == 404
        assert (await client.get('/public/orders/tracking/invalid')).status_code == 404
        async with database() as session:
            order = await session.get(Order, oid)
            token = order.guest_tracking_url.rsplit('/', 1)[1]
        tracked = await client.get('/public/orders/tracking/' + token)
        assert tracked.status_code == 200
        assert tracked.json()['shipments'][0]['tracking_number'].startswith('MOCK-')
        assert 'address_snapshot' not in tracked.text and 'provider_reference' not in tracked.text



async def test_replay_after_mode_off_keeps_permission_gate(database, checkout_dependencies, monkeypatch):
    oid, _ = await setup_order(database, checkout_dependencies)
    key = str(uuid4())
    a = await create(database, oid, key=key)
    monkeypatch.setattr(settings, 'np_mock_shipments_enabled', False)
    replay = await create(database, oid, key=key)
    assert replay.model_dump() == a.model_dump()
    with pytest.raises(HTTPException) as error:
        await create(database, oid)
    assert error.value.detail['code'] == 'mock_shipments_disabled'
    async with database() as session:
        actor = SimpleNamespace(id=1, is_superuser=False, can_manage_shipments=False, can_post_inventory=True)
        with pytest.raises(HTTPException) as error:
            await shipments.create(session, actor, oid, ShipmentCreate(expected_revision=1, packaging=PACK), key)
        assert error.value.status_code == 403



async def test_migrated_snapshot_trigger_allows_status_and_blocks_json_edits(database, checkout_dependencies):
    """Execute actual migration trigger DDL; metadata.create_all omits triggers."""
    import ast
    from pathlib import Path
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    migration = ast.parse(Path('alembic/versions/0113_mock_np_shipments.py').read_text())
    trigger_ddl = []
    for node in ast.walk(migration):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'execute' and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            sql = node.args[0].value
            if sql.startswith('CREATE FUNCTION protect_shipment_snapshot') or sql.startswith('CREATE TRIGGER immutable_shipment_snapshot'):
                trigger_ddl.append(sql)
    assert len(trigger_ddl) == 2
    oid, _ = await setup_order(database, checkout_dependencies)
    created = await create(database, oid)
    sid = created.shipments[0].id
    async with database() as session:
        for sql in trigger_ddl:
            await session.execute(text(sql))
        await session.commit()
    # Real command updates status, timestamp and order revision through trigger.
    refreshed = await command(database, oid, sid, created.revision)
    assert refreshed.shipments[0].status == 'created'
    for column in ('address_snapshot', 'packaging'):
        async with database() as session:
            with pytest.raises(DBAPIError, match='Shipment snapshot is immutable'):
                await session.execute(text(f"UPDATE order_shipments SET {column} = '{{}}'::json WHERE id = :sid"), {'sid': sid})
            await session.rollback()
    async with database() as session:
        stored = await session.get(Shipment, sid)
        assert stored.address_snapshot == ADDRESS
        assert stored.packaging == PACK
