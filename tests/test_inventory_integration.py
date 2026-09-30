"""Real PostgreSQL inventory contracts in an isolated opt-in schema."""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import all_models  # noqa: F401
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryCount, InventoryMovement, InventoryMovementType, InventoryReceipt
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
from app.models.product import Product
from app.schemas.order import OrderCreate
from app.services.inventory import InventoryService
from app.services.order import OrderService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def database():
    url = os.environ.get("INVENTORY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set INVENTORY_TEST_DATABASE_URL to an isolated local PostgreSQL database")
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1", "::1"} or "test" not in (parsed.database or ""):
        pytest.fail("Integration database must be local and have 'test' in its database name")
    schema = "inventory_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(
                AdminUser(
                    id=1,
                    email=f"inventory-{uuid4().hex}@example.test",
                    hashed_password="not-used",
                    is_active=True,
                    is_superuser=True,
                )
            )
            await session.commit()
        yield sessions
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def checkout_payload(product_id: int) -> OrderCreate:
    return OrderCreate(firstName="Jane", phoneNumber="+380501112233", items=[{"productId": product_id, "quantity": 1}])


@pytest.fixture
def checkout_dependencies(monkeypatch):
    async def visibility(_cls, _session):
        return SimpleNamespace(
            product_states=lambda products: {item.id: SimpleNamespace(is_effectively_visible=True) for item in products},
            category_parents=lambda: {},
        )

    async def prices(_session, products, **_kwargs):
        return {
            item.id: SimpleNamespace(
                base_price=Decimal("100.00"), price=Decimal("100.00"), discount_amount=Decimal("0.00"),
                promotion_id=None, promotion_name=None, promotion_code=None,
            )
            for item in products
        }

    monkeypatch.setattr("app.services.order.CatalogVisibility.load", classmethod(visibility))
    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", prices)


async def add_product(database, *, stock: int, reserved: int = 0, backorder: bool = False) -> Product:
    async with database() as session:
        item = Product(name="Inventory product", slug="inventory-product", price=Decimal("100.00"),
                       stock_quantity=stock, reserved_quantity=reserved, allow_backorder=backorder)
        session.add(item)
        await session.commit()
        return item


@pytest.mark.anyio
async def test_simultaneous_checkout_reserves_only_one_unit(database, checkout_dependencies):
    item = await add_product(database, stock=1)

    async def attempt():
        async with database() as session:
            try:
                return await OrderService().create_order(session, checkout_payload(item.id))
            except HTTPException as error:
                return error.status_code

    first, second = await asyncio.gather(attempt(), attempt())
    assert sum(isinstance(result, int) and result == 409 for result in (first, second)) == 1
    assert sum(not isinstance(result, int) for result in (first, second)) == 1
    async with database() as session:
        refreshed = await session.get(Product, item.id)
        assert (refreshed.stock_quantity, refreshed.reserved_quantity) == (1, 1)
        assert await session.scalar(
            select(func.count()).select_from(InventoryMovement).where(
                InventoryMovement.product_id == item.id,
                InventoryMovement.movement_type == InventoryMovementType.reservation,
            )
        ) == 1


@pytest.mark.anyio
async def test_partial_linked_receipt_reserves_received_stock_once(database):
    item = await add_product(database, stock=0, backorder=True)
    async with database() as session:
        order = Order(customer_name="Jane", customer_phone="+380501112233", total_amount=Decimal("300.00"),
                      status=OrderStatus.paid)
        line = OrderItem(product_id=item.id, quantity=3, quantity_from_stock=0, quantity_to_order=3,
                         quantity_received_for_order=0, procurement_status=ProcurementStatus.ordered,
                         price=Decimal("100.00"))
        order.items = [line]
        session.add(order)
        await session.commit()
        order_id, item_id = order.id, line.id

    service = InventoryService()
    async with database() as session:
        receipt = await service.create_receipt(session, reason="Supplier shipment", comment=None, idempotency_key="receipt-1")
        await service.add_receipt_item(
            session, receipt.id, product_id=item.id, barcode=None, quantity=2,
            purchase_unit_cost=Decimal("50.00"), batch_number=None, expiration_date=None,
            allocations=[{"order_item_id": item_id, "quantity": 2}],
        )
        posted = await service.post_receipt(session, receipt.id, admin_user_id=1, idempotency_key="receipt-1")
        assert await service.post_receipt(session, receipt.id, admin_user_id=1, idempotency_key="receipt-1") is posted

    async with database() as session:
        refreshed = await session.get(Product, item.id)
        line = await session.get(OrderItem, item_id)
        assert (refreshed.stock_quantity, refreshed.reserved_quantity) == (2, 2)
        assert (line.quantity_received_for_order, line.procurement_status) == (2, ProcurementStatus.ordered)
        movements = list(await session.scalars(select(InventoryMovement).where(InventoryMovement.product_id == item.id)))
        assert [(row.movement_type, row.on_hand_delta, row.reserved_delta) for row in movements] == [
            (InventoryMovementType.receipt, 2, 0),
            (InventoryMovementType.reservation, 0, 2),
        ]
        assert await session.get(Order, order_id) is not None


@pytest.mark.anyio
async def test_posted_count_adjusts_on_hand_once_without_violating_reservation(database):
    item = await add_product(database, stock=5, reserved=2)
    service = InventoryService()
    async with database() as session:
        count = await service.create_count(session, reason="Cycle count", idempotency_key="count-1")
        await service.set_count_item(session, count.id, product_id=item.id, barcode=None, counted_quantity=7)
        posted = await service.post_count(session, count.id, admin_user_id=1, idempotency_key="count-1")
        assert await service.post_count(session, count.id, admin_user_id=1, idempotency_key="count-1") is posted

    async with database() as session:
        refreshed = await session.get(Product, item.id)
        assert (refreshed.stock_quantity, refreshed.reserved_quantity) == (7, 2)
        count = (await session.scalars(select(InventoryCount))).one()
        movements = list(await session.scalars(select(InventoryMovement).where(InventoryMovement.inventory_count_id == count.id)))
        assert len(movements) == 1
        assert (movements[0].movement_type, movements[0].on_hand_delta, movements[0].reserved_delta) == (
            InventoryMovementType.inventory_adjustment, 2, 0,
        )


@pytest.mark.anyio
async def test_receipt_post_and_order_cancellation_finish_without_deadlock(database):
    item = await add_product(database, stock=0, backorder=True)
    async with database() as session:
        order = Order(
            customer_name="Jane",
            customer_phone="+380501112233",
            total_amount=Decimal("100.00"),
            status=OrderStatus.paid,
        )
        line = OrderItem(
            product_id=item.id,
            quantity=1,
            quantity_from_stock=0,
            quantity_to_order=1,
            quantity_received_for_order=0,
            procurement_status=ProcurementStatus.ordered,
            price=Decimal("100.00"),
        )
        order.items = [line]
        session.add(order)
        await session.commit()
        order_id, item_id = order.id, line.id

    service = InventoryService()
    async with database() as session:
        receipt = await service.create_receipt(
            session,
            reason="Concurrent supplier shipment",
            comment=None,
            idempotency_key="concurrent-receipt-create",
        )
        await service.add_receipt_item(
            session,
            receipt.id,
            product_id=item.id,
            barcode=None,
            quantity=1,
            purchase_unit_cost=Decimal("50.00"),
            batch_number=None,
            expiration_date=None,
            allocations=[{"order_item_id": item_id, "quantity": 1}],
        )
        receipt_id = receipt.id

    async def post_receipt():
        async with database() as session:
            try:
                return await service.post_receipt(
                    session,
                    receipt_id,
                    admin_user_id=1,
                    idempotency_key="concurrent-receipt-post",
                )
            except HTTPException as error:
                return error

    async def cancel_order():
        async with database() as session:
            try:
                return await OrderService().update_status(
                    session,
                    order_id,
                    OrderStatus.cancelled,
                    admin_user_id=1,
                )
            except HTTPException as error:
                return error

    posted, cancelled = await asyncio.wait_for(
        asyncio.gather(post_receipt(), cancel_order()),
        timeout=5,
    )
    assert not isinstance(cancelled, HTTPException)
    assert not isinstance(posted, Exception) or (
        isinstance(posted, HTTPException) and posted.status_code == 409
    )

    async with database() as session:
        refreshed = await session.get(Product, item.id)
        order = await session.get(Order, order_id)
        receipt = await session.get(InventoryReceipt, receipt_id)
        assert order.status == OrderStatus.cancelled
        assert refreshed.reserved_quantity == 0
        if isinstance(posted, HTTPException):
            assert refreshed.stock_quantity == 0
            assert receipt.status.value == "draft"
        else:
            assert refreshed.stock_quantity == 1
            assert receipt.status.value == "posted"
