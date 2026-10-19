"""Explicit mock COD and TTN receipts on disposable localhost PostgreSQL schemas."""
import asyncio
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryMovement, MovementType
from app.models.mock_cod import MockCODConfirmation, MockShipmentReturnReceipt
from app.models.order import Order, OrderHandover, OrderPayment
from app.models.product import Product
from app.schemas.mock_cod import MockCODPaymentCreate, MockParcelReceiptCreate, MockReturnShipmentCreate
from app.schemas.shipment import ShipmentCreate
from app.services import local_sales, mock_cod, shipments
from app.services.inventory import InventoryService
from app.services.order import OrderService
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, checkout_payload, add_product
from tests.test_shipments_integration import setup_order, create, command, PACK, ADDRESS

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def enable_mock(monkeypatch):
    monkeypatch.setattr(settings, 'np_mock_shipments_enabled', True)
    monkeypatch.setattr(settings, 'app_env', 'test')


async def delivered(database, checkout_dependencies):
    oid, pid = await setup_order(database, checkout_dependencies, stock=5)
    order = await create(database, oid)
    sid = order.shipments[0].id
    order = await command(database, oid, sid, order.revision, 'handover', evidence='Synthetic outbound handover')
    order = await command(database, oid, sid, order.revision, mock_status='delivered')
    return order, sid, pid


def cod_payload(order, **kwargs):
    return MockCODPaymentCreate(expected_revision=order.revision, evidence='Synthetic COD settlement',
        confirm=True, provider_event_id=kwargs.pop('provider_event_id', 'MOCK-COD-' + uuid4().hex), **kwargs)


async def confirm(database, order, sid, *, key=None, payload=None):
    async with database() as session:
        return await mock_cod.confirm_cod(session, await session.get(AdminUser, 1), order.id, sid,
            payload or cod_payload(order), key or str(uuid4()))


async def receive(database, order, sid, *, key=None, disposition='restock', payload=None):
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        summary = await mock_cod.settlement(session, actor, order.id, sid)
        payload = payload or MockParcelReceiptCreate(expected_revision=order.revision, evidence='Synthetic warehouse inspection',
            confirm=True, items=[{'order_item_id':i.order_item_id, 'quantity':i.quantity, 'disposition':disposition} for i in summary.items])
        return await mock_cod.receive_return(session, actor, order.id, sid, payload, key or str(uuid4()))


async def buyer_return(database, order, sid, *, items=None, key=None):
    async with database() as session:
        return await mock_cod.create_return_shipment(session, await session.get(AdminUser, 1), order.id, sid,
            MockReturnShipmentCreate(expected_revision=order.revision, evidence='Synthetic buyer return carrier acceptance',
                confirm=True, items=items), key or str(uuid4()))


async def stock(database, pid):
    async with database() as session:
        p = await session.get(Product, pid)
        return p.stock_quantity, p.reserved_quantity


async def test_shop_cod_is_explicit_exact_and_preserves_checkout_origin(database, checkout_dependencies):
    order, sid, pid = await delivered(database, checkout_dependencies)
    assert order.payment_status == 'unpaid' and await stock(database,pid) == (4,0)
    key, payload = str(uuid4()), cod_payload(order)
    paid = await confirm(database,order,sid,key=key,payload=payload)
    assert paid.payment_status == 'simulated_paid'
    assert paid.channel == 'shop' and paid.simulation is False
    assert await confirm(database,order,sid,key=key,payload=payload) == paid
    async with database() as session:
        row = await session.get(Order,order.id)
        assert mock_cod.validated_cod_snapshot(row,row.payments[0])['provider_event_id'] == payload.provider_event_id
        assert (row.payments[0].amount_minor,row.payments[0].source,row.payments[0].method) == (10000,'mock_cod_confirmation','cod')
        assert await session.scalar(select(func.count()).select_from(MockCODConfirmation)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
    assert await stock(database,pid) == (4,0)


async def test_delivered_never_confirms_cod_without_explicit_command(database,checkout_dependencies):
    order,sid,_ = await delivered(database,checkout_dependencies)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        summary = await mock_cod.settlement(session,await session.get(AdminUser,1),order.id,sid)
        assert summary.cod_payment is None and 'confirm_cod' in summary.allowed_actions
    with pytest.raises(ValidationError):
        MockCODPaymentCreate(expected_revision=order.revision,evidence='x',confirm=False,provider_event_id='MOCK-COD-no')
    with pytest.raises(ValidationError):
        MockCODPaymentCreate(**cod_payload(order).model_dump(), amount_minor=1)


async def test_cod_rejects_pre_delivery_and_changed_server_total(database,checkout_dependencies):
    oid,_ = await setup_order(database,checkout_dependencies)
    order = await create(database,oid)
    sid = order.shipments[0].id
    with pytest.raises(HTTPException) as error:
        await confirm(database,order,sid)
    assert error.value.detail['code'] == 'handover_required'
    order = await command(database,oid,sid,order.revision,'handover',evidence='Synthetic handover')
    with pytest.raises(HTTPException) as error:
        await confirm(database,order,sid)
    assert error.value.detail['code'] == 'delivered_shipment_required'
    order = await command(database,oid,sid,order.revision,mock_status='delivered')
    async with database() as session:
        row = await session.get(Order,oid)
        row.total_amount = 99
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await confirm(database,order,sid)
    assert error.value.detail['code'] == 'cod_amount_mismatch'


async def test_concurrent_different_keys_create_only_one_cod_payment(database,checkout_dependencies):
    order,sid,_ = await delivered(database,checkout_dependencies)
    async def attempt():
        try:
            return await confirm(database,order,sid)
        except HTTPException as error:
            return error.detail['code']
    results = await asyncio.gather(attempt(),attempt())
    assert sum(isinstance(r,str) for r in results) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(MockCODConfirmation)) == 1


async def test_provider_event_identity_cannot_pay_two_orders_under_race(database,checkout_dependencies):
    first,sid1,pid = await delivered(database,checkout_dependencies)
    async with database() as session:
        row = await OrderService().create_order(session,checkout_payload(pid))
        row.fulfillment_method,row.shipping_payload_json = 'nova_poshta',ADDRESS.copy()
        await session.commit()
        oid2 = row.id
    second = await create(database,oid2)
    sid2 = second.shipments[0].id
    second = await command(database,oid2,sid2,second.revision,'handover',evidence='Synthetic second handover')
    second = await command(database,oid2,sid2,second.revision,mock_status='delivered')
    event = 'MOCK-COD-' + uuid4().hex
    async def attempt(order,sid):
        try:
            return await confirm(database,order,sid,payload=cod_payload(order,provider_event_id=event))
        except HTTPException as error:
            return error.detail['code']
    results = await asyncio.gather(attempt(first,sid1),attempt(second,sid2))
    assert results.count('cod_payment_exists') == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockCODConfirmation)) == 1


@pytest.mark.parametrize('disposition,expected',[('restock',5),('not_restock',4)])
async def test_refused_return_receipt_is_separate_immutable_fact(database,checkout_dependencies,disposition,expected):
    oid,pid = await setup_order(database,checkout_dependencies,stock=5)
    order = await create(database,oid)
    sid = order.shipments[0].id
    order = await command(database,oid,sid,order.revision,'handover',evidence='Synthetic handover')
    for status in ['refused','returning','returned']:
        order = await command(database,oid,sid,order.revision,mock_status=status)
    assert await stock(database,pid) == (4,0)
    async with database() as session:
        summary = await mock_cod.settlement(session,await session.get(AdminUser,1),oid,sid)
    payload = MockParcelReceiptCreate(expected_revision=order.revision,evidence='Synthetic inspection',confirm=True,
        items=[{'order_item_id':summary.items[0].order_item_id,'quantity':1,'disposition':disposition}])
    key = str(uuid4())
    received = await receive(database,order,sid,payload=payload,key=key)
    assert await receive(database,order,sid,payload=payload,key=key) == received
    assert await stock(database,pid) == (expected,0)
    with pytest.raises(HTTPException):
        await receive(database,received,sid,disposition=disposition)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        assert await session.scalar(select(func.count()).select_from(MockShipmentReturnReceipt)) == 1


async def test_buyer_return_creates_linked_ttn_without_repeating_handover_or_stock(database,checkout_dependencies):
    order,sid,pid = await delivered(database,checkout_dependencies)
    order = await confirm(database,order,sid)
    order = await buyer_return(database,order,sid)
    child = next(s for s in order.shipments if s.id != sid)
    assert child.source == 'mock_return' and child.status == 'returning'
    assert await stock(database,pid) == (4,0)
    async with database() as session:
        row = await session.get(Order,order.id)
        assert mock_cod.validated_cod_snapshot(row,row.payments[0]) is not None
        returned_row = next(s for s in row.shipments if s.id == child.id)
        assert returned_row.return_evidence == 'Synthetic buyer return carrier acceptance'
        assert returned_row.return_operator_id == 1
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
    with pytest.raises(HTTPException) as error:
        await receive(database,order,child.id)
    assert error.value.detail['code'] == 'returned_shipment_required'
    order = await command(database,order.id,child.id,order.revision,mock_status='returned')
    received = await receive(database,order,child.id)
    assert received.payment_status == 'simulated_paid' and await stock(database,pid) == (5,0)


async def test_generic_customer_return_cannot_bypass_parcel_receipt_with_item_only_reference(database,checkout_dependencies):
    order,sid,pid = await delivered(database,checkout_dependencies)
    async with database() as session:
        item = (await local_sales.load(session,order.id)).items[0]
        with pytest.raises(HTTPException) as error:
            await InventoryService().manual_operation(session, product_id=pid,movement_type=MovementType.customer_return,
                quantity=1,reason='Synthetic bypass',admin_user_id=1,idempotency_key=str(uuid4()),order_item_id=item.id)
        assert error.value.status_code == 409
    assert await stock(database,pid) == (4,0)


async def test_local_gate_and_current_operator_permission_reject_mutation(database,checkout_dependencies,monkeypatch):
    order,sid,_ = await delivered(database,checkout_dependencies)
    monkeypatch.setattr(settings,'np_mock_shipments_enabled',False)
    with pytest.raises(HTTPException) as error:
        await confirm(database,order,sid)
    assert error.value.detail['code'] == 'mock_shipments_disabled'
    monkeypatch.setattr(settings,'np_mock_shipments_enabled',True)
    async with database() as session:
        actor = await session.get(AdminUser,1)
        actor.is_superuser=False
        actor.can_record_sales=True
        actor.can_manage_shipments=False
        await session.commit()
        with pytest.raises(HTTPException) as error:
            await mock_cod.confirm_cod(session,actor,order.id,sid,cod_payload(order),str(uuid4()))
        assert error.value.status_code == 403


async def test_partial_return_parcel_restores_only_selected_quantity(database,checkout_dependencies):
    product = await add_product(database,stock=5)
    payload = checkout_payload(product.id)
    payload.items[0].quantity = 3
    async with database() as session:
        row = await OrderService().create_order(session,payload)
        row.fulfillment_method,row.shipping_payload_json = 'nova_poshta',ADDRESS.copy()
        await session.commit()
        oid = row.id
    order = await create(database,oid,ShipmentCreate(expected_revision=1,packaging={**PACK,'cod_amount_minor':30000,'declared_value_minor':30000}))
    sid = order.shipments[0].id
    order = await command(database,oid,sid,order.revision,'handover',evidence='Synthetic full handover')
    order = await command(database,oid,sid,order.revision,mock_status='delivered')
    async with database() as session:
        item_id = (await local_sales.load(session,oid)).items[0].id
    order = await buyer_return(database,order,sid,items=[{'order_item_id':item_id,'quantity':1}])
    child = next(s for s in order.shipments if s.id != sid)
    order = await command(database,oid,child.id,order.revision,mock_status='returned')
    key = str(uuid4())
    async def attempt():
        try:
            return await receive(database,order,child.id,key=key)
        except HTTPException as error:
            return error.detail['code']
    a,b = await asyncio.gather(attempt(),attempt())
    assert a == b and await stock(database,product.id) == (3,0)
    async with database() as session:
        assert await session.scalar(select(func.sum(InventoryMovement.on_hand_delta)).where(
            InventoryMovement.movement_type == MovementType.customer_return)) == 1


@pytest.fixture
async def migrated_database(database):
    """Apply the real migration atop an otherwise equivalent empty predecessor."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/0121_mock_cod_settlement.py'
    spec = importlib.util.spec_from_file_location('mock_cod_migration',path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with database.kw['bind'].begin() as connection:
        await connection.execute(text('ALTER TABLE mock_return_receipts DROP COLUMN shipment_return_receipt_id'))
        await connection.execute(text('DROP TABLE mock_cod_confirmations'))
        await connection.execute(text('DROP TABLE mock_shipment_return_receipts'))
        await connection.execute(text('DROP INDEX uq_mock_return_original_shipment'))
        await connection.execute(text('ALTER TABLE order_shipments DROP COLUMN return_operator_id'))
        await connection.execute(text('ALTER TABLE order_shipments DROP COLUMN return_evidence'))
        await connection.execute(text("CREATE FUNCTION prevent_sales_fact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Immutable sales fact'; END $$"))
        def upgrade(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                migration.upgrade()
        await connection.run_sync(upgrade)
    return database


async def test_migration_enforces_durable_cod_provenance_and_fact_immutability(migrated_database,checkout_dependencies):
    database = migrated_database
    order,sid,_ = await delivered(database,checkout_dependencies)
    paid = await confirm(database,order,sid)
    assert paid.payment_status == 'simulated_paid'
    async with database() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE mock_cod_confirmations SET evidence='altered' WHERE order_id=:id"),{'id':order.id})
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE orders SET simulation=true WHERE id=:id"),{'id':order.id})
        await session.rollback()
        with pytest.raises(IntegrityError):
            columns = 'order_id,shipment_id,payment_id,provider_event_id,tracking_number,shipment_uuid,amount_minor,original_channel,original_simulation,operator_id,evidence,created_at'
            await session.execute(text(f'INSERT INTO mock_cod_confirmations ({columns}) SELECT {columns} FROM mock_cod_confirmations'))
        await session.rollback()
        row = await OrderService().create_order(session,checkout_payload((await local_sales.load(session,order.id)).items[0].product_id))
        with pytest.raises(DBAPIError):
            await session.execute(text("INSERT INTO order_payments (order_id,amount_minor,method,source,evidence,operator_id,simulation,created_at) VALUES (:id,10000,'cod','mock_cod_confirmation','forged',1,true,now())"),{'id':row.id})
            await session.commit()
        await session.rollback()


async def test_migration_validates_returned_parcel_and_protects_receipt_history(migrated_database,checkout_dependencies):
    database = migrated_database
    order,sid,pid = await delivered(database,checkout_dependencies)
    order = await buyer_return(database,order,sid)
    child = next(s for s in order.shipments if s.id != sid)
    order = await command(database,order.id,child.id,order.revision,mock_status='returned')
    await receive(database,order,child.id)
    assert await stock(database,pid) == (5,0)
    async with database() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text('DELETE FROM mock_shipment_return_receipts WHERE order_id=:id'),{'id':order.id})
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE order_shipments SET return_evidence='changed' WHERE id=:id"),{'id':child.id})
        await session.rollback()


async def test_downgrade_preserves_return_consent_before_receipt(migrated_database,checkout_dependencies):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    order,sid,_ = await delivered(migrated_database,checkout_dependencies)
    await buyer_return(migrated_database,order,sid)
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/0121_mock_cod_settlement.py'
    spec = importlib.util.spec_from_file_location('mock_cod_downgrade',path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with migrated_database.kw['bind'].begin() as connection:
        def downgrade(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                with pytest.raises(RuntimeError,match='carrier consent'):
                    migration.downgrade()
        await connection.run_sync(downgrade)
