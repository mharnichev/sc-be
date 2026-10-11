"""Mobile scanner flow, barcode races and posting retries on PostgreSQL."""
import asyncio
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.inventory import InventoryMovement, InventoryReceipt
from app.models.product import Product
from app.schemas.inventory import InventoryProductCreateRequest, InventoryProductResponse
from app.services.inventory import InventoryService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401

pytestmark = pytest.mark.anyio


async def seed(database, **kwargs):
    async with database() as session:
        item = Product(name='Scanner Shampoo', slug=uuid4().hex, sku=uuid4().hex,
                       price=Decimal('100'), stock_quantity=0, reserved_quantity=0, **kwargs)
        session.add(item)
        await session.commit()
        return item.id


async def test_unknown_scan_search_bind_exact_and_no_stock_change(database):
    product_id = await seed(database)
    service = InventoryService()
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.get_product_by_barcode(session, '00123abc')
        assert error.value.status_code == 404
        items, total = await service.list_stock(session, page=1, page_size=1, search='Shampoo')
        assert total == 1 and items[0].id == product_id
        sku = items[0].sku
        assert (await service.list_stock(session, page=1, page_size=1, search=sku))[1] == 1
        assert (await service.list_stock(session, page=2, page_size=1, search='Shampoo'))[0] == []
        bound = await service.bind_barcode(session, product_id, '00123abc', 1)
        assert InventoryProductResponse.model_validate(bound).barcode == '00123abc'
        assert bound.barcode_bound_by_user_id == 1
        again = await service.bind_barcode(session, product_id, '00123abc', 1)
        assert again.id == product_id and again.stock_quantity == 0
        assert (await service.get_product_by_barcode(session, '00123abc')).id == product_id
        for other_code in ('00123ABC', '123abc'):
            with pytest.raises(HTTPException) as error:
                await service.get_product_by_barcode(session, other_code)
            assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await service.bind_barcode(session, product_id, '999', 1)
        assert error.value.status_code == 409


async def test_concurrent_barcode_bind_has_one_winner(database):
    first, second = await seed(database), await seed(database)
    async def attempt(product_id):
        async with database() as session:
            try:
                return (await InventoryService().bind_barcode(session, product_id, '0000001', 1)).id
            except HTTPException as error:
                return error.status_code
    results = await asyncio.wait_for(asyncio.gather(attempt(first), attempt(second)), timeout=10)
    assert results.count(409) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Product).where(Product.barcode == '0000001')) == 1
        assert await session.scalar(select(func.sum(Product.stock_quantity))) == 0


async def test_create_retry_then_concurrent_post_changes_stock_once(database):
    service = InventoryService()
    data = InventoryProductCreateRequest(name='New Shampoo', barcode='00000987', package_size='250 ml', volume_ml=250).model_dump(mode='json')
    async def create():
        async with database() as session:
            return (await service.create_product_card(session, data=data, admin_user_id=1, idempotency_key='card-1')).id
    ids = await asyncio.wait_for(asyncio.gather(create(), create()), timeout=10)
    assert ids[0] == ids[1]
    async with database() as session:
        product = await session.get(Product, ids[0])
        assert product.stock_quantity == 0 and not product.is_active
        assert product.created_by_user_id == product.barcode_bound_by_user_id == 1
        with pytest.raises(HTTPException) as error:
            await service.create_product_card(session, data={**data, 'name': 'Different'}, admin_user_id=1, idempotency_key='card-1')
        assert error.value.status_code == 409
        receipt = await service.create_receipt(session, reason='Delivery', comment=None, idempotency_key='receipt-new')
        receipt_id = receipt.id
        params = dict(product_id=ids[0], barcode=None, quantity=3, purchase_unit_cost=Decimal('24.50'),
                      batch_number='LOT-1', expiration_date=date(2027, 1, 1), allocations=[], idempotency_key='scan-1')
        await service.add_receipt_item(session, receipt_id, **params)
        draft = await service.add_receipt_item(session, receipt_id, **params)
        assert len(draft.items) == 1 and draft.items[0].quantity == 3
        assert (await session.get(Product, ids[0])).stock_quantity == 0
    async def post(key):
        async with database() as session:
            return (await service.post_receipt(session, receipt_id, 1, key)).status
    await asyncio.wait_for(asyncio.gather(post('post-a'), post('post-b')), timeout=10)
    async with database() as session:
        product = await session.get(Product, ids[0])
        assert (product.stock_quantity, product.reserved_quantity) == (3, 0)
        receipt = await service.get_receipt(session, receipt_id)
        assert receipt.posted_by_user_id == 1
        line = receipt.items[0]
        assert (line.purchase_unit_cost, line.batch_number, line.expiration_date) == (Decimal('24.50'), 'LOT-1', date(2027, 1, 1))
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.receipt_id == receipt_id)) == 1


async def test_post_revalidates_total_allocation_across_lots_atomically(database):
    from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
    product_id = await seed(database)
    service = InventoryService()
    async with database() as session:
        order = Order(customer_name='Test', customer_phone='+380501112233',
                      total_amount=Decimal('500'), status=OrderStatus.paid)
        order.items = [OrderItem(product_id=product_id, quantity=5, quantity_from_stock=0,
            quantity_to_order=5, quantity_received_for_order=0, procurement_status=ProcurementStatus.ordered,
            price=Decimal('100'))]
        session.add(order)
        await session.commit()
        order_item_id = order.items[0].id
        first = await service.create_receipt(session, reason='Two lots', comment=None, idempotency_key='lots')
        first_id = first.id
        for lot in ('A', 'B'):
            await service.add_receipt_item(session, first_id, product_id=product_id, barcode=None,
                quantity=2, purchase_unit_cost=Decimal('10'), batch_number=lot, expiration_date=None,
                allocations=[dict(order_item_id=order_item_id, quantity=2)], idempotency_key=lot)
        second = await service.create_receipt(session, reason='Other supply', comment=None, idempotency_key='other')
        await service.add_receipt_item(session, second.id, product_id=product_id, barcode=None,
            quantity=2, purchase_unit_cost=Decimal('10'), batch_number='C', expiration_date=None,
            allocations=[dict(order_item_id=order_item_id, quantity=2)], idempotency_key='C')
        await service.post_receipt(session, second.id, 1, 'post-other')
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.post_receipt(session, first_id, 1, 'post-lots')
        assert error.value.status_code == 409
        await session.rollback()
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (2, 2)
        assert (await session.get(OrderItem, order_item_id)).quantity_received_for_order == 2
        assert (await session.get(InventoryReceipt, first_id)).status.value == 'draft'
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.receipt_id == first_id)) == 0


async def test_migration_roundtrip_preserves_admin_permission_and_denies_master(database):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.models.admin_user import AdminUser
    from app.models.booking import Master
    from sqlalchemy import text

    spec = importlib.util.spec_from_file_location('mobile_inventory_migration',
        Path(__file__).parents[1] / 'alembic/versions/0107_mobile_inventory.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with database() as session:
        session.add(AdminUser(id=2, email='master@example.test', hashed_password='unused', is_superuser=False))
        session.add(AdminUser(id=3, email='admin@example.test', hashed_password='unused', is_superuser=False))
        await session.flush()
        session.add(Master(full_name='Master', admin_user_id=2))
        await session.commit()
        connection = await session.connection()
        def roundtrip(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                migration.downgrade()
                migration.upgrade()
        await connection.run_sync(roundtrip)
        await session.commit()
        permissions = dict((await session.execute(text('SELECT id, can_post_inventory FROM admin_users'))).all())
        assert permissions == {1: True, 2: False, 3: True}
        session.add(AdminUser(id=4, email='new@example.test', hashed_password='unused'))
        await session.commit()
        assert not await session.scalar(text('SELECT can_post_inventory FROM admin_users WHERE id=4'))
