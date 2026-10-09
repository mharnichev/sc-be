"""Product deletion preserves history in isolated, opt-in PostgreSQL schemas."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.inventory import (
    InventoryCount,
    InventoryCountItem,
    InventoryMovement,
    InventoryMovementType,
    InventoryReceipt,
    InventoryReceiptItem,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shop import ProductImage
from app.services.product import ProductService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


HISTORY_RELATIONSHIPS = {
    "movement": Product.inventory_movements,
    "receipt": Product.inventory_receipt_items,
    "count": Product.inventory_count_items,
}
PRESERVED_MODELS = (
    Product, ProductImage, Order, OrderItem, InventoryMovement,
    InventoryReceipt, InventoryReceiptItem, InventoryCount, InventoryCountItem,
)


async def seed_product(database) -> int:
    async with database() as session:
        product = Product(
            name="Deletion history product", slug="deletion-history-product",
            price=Decimal("100.00"), stock_quantity=7, reserved_quantity=0,
        )
        product.images = [ProductImage(image_url="/media/synthetic-deletion.png", alt="Preserve on failure")]
        session.add(product)
        await session.commit()
        return product.id


async def add_history(session, product_id: int, kind: str) -> None:
    if kind == "movement":
        session.add(InventoryMovement(
            product_id=product_id, movement_type=InventoryMovementType.opening_balance,
            quantity=7, on_hand_delta=7, reserved_delta=0,
            reference_key="deletion-history-opening", reason="Synthetic opening balance",
        ))
    elif kind == "receipt":
        receipt = InventoryReceipt(reason="Synthetic draft receipt")
        receipt.items = [InventoryReceiptItem(
            product_id=product_id, quantity=2, purchase_unit_cost=Decimal("25.00"),
            batch_number="synthetic-batch",
        )]
        session.add(receipt)
    elif kind == "count":
        count = InventoryCount(reason="Synthetic draft count")
        count.items = [InventoryCountItem(product_id=product_id, expected_quantity=7, counted_quantity=9)]
        session.add(count)
    else:
        raise AssertionError(f"Unknown synthetic history kind: {kind}")
    await session.flush()


async def snapshot(session) -> dict[str, list[tuple]]:
    """Read every persisted column without populating ORM relationship caches."""
    return {
        model.__tablename__: [tuple(row) for row in (
            await session.execute(select(model.__table__).order_by(model.id))
        ).all()]
        for model in PRESERVED_MODELS
    }


@pytest.mark.anyio
@pytest.mark.parametrize("kind", HISTORY_RELATIONSHIPS)
@pytest.mark.parametrize("loaded", [False, True], ids=["unloaded", "loaded"])
async def test_inventory_history_alone_rejects_product_deletion_without_changes(database, kind, loaded):
    product_id = await seed_product(database)
    async with database() as session:
        await add_history(session, product_id, kind)
        await session.commit()
        before = await snapshot(session)
        assert before["order_items"] == []

    async with database() as session:
        statement = select(Product).where(Product.id == product_id)
        if loaded:
            statement = statement.options(selectinload(HISTORY_RELATIONSHIPS[kind]))
        product = (await session.scalars(statement)).one()
        with pytest.raises(HTTPException) as error:
            await ProductService().delete_product(session, product)
        assert error.value.status_code == 400
        assert "inventory" in error.value.detail.lower()
        # A controlled rejection must leave the session usable.
        assert await session.scalar(select(Product.id).where(Product.id == product_id)) == product_id

    async with database() as session:
        assert await snapshot(session) == before


@pytest.mark.anyio
async def test_order_history_keeps_existing_product_deletion_error_and_rows(database):
    product_id = await seed_product(database)
    async with database() as session:
        order = Order(
            customer_name="Synthetic customer", customer_phone="+380500000001",
            total_amount=Decimal("100.00"),
        )
        order.items = [OrderItem(
            product_id=product_id, quantity=1, quantity_from_stock=1, quantity_to_order=0,
            price=Decimal("100.00"), product_name="Saved product name",
        )]
        session.add(order)
        await session.commit()
        before = await snapshot(session)

    async with database() as session:
        product = await session.get(Product, product_id)
        with pytest.raises(HTTPException) as error:
            await ProductService().delete_product(session, product)
        assert error.value.status_code == 400
        assert error.value.detail == "Product is linked to existing orders and cannot be deleted"

    async with database() as session:
        assert await snapshot(session) == before


@pytest.mark.anyio
async def test_product_without_order_or_inventory_history_is_hard_deleted(database):
    product_id = await seed_product(database)
    async with database() as session:
        product = await session.get(Product, product_id)
        await ProductService().delete_product(session, product)

    async with database() as session:
        assert await session.get(Product, product_id) is None
        assert all(not rows for rows in (await snapshot(session)).values())


@pytest.mark.anyio
@pytest.mark.parametrize("kind", HISTORY_RELATIONSHIPS)
async def test_concurrent_inventory_reference_rejects_delete_and_rolls_back_cascades(database, kind):
    product_id = await seed_product(database)
    pid_ready = asyncio.Event()
    delete_pid = None

    async def delete():
        nonlocal delete_pid
        async with database() as session:
            product = await session.get(Product, product_id)
            delete_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            pid_ready.set()
            with pytest.raises(HTTPException) as error:
                await ProductService().delete_product(session, product)
            assert error.value.status_code == 400
            assert "inventory" in error.value.detail.lower()
            assert await session.scalar(select(Product.id).where(Product.id == product_id)) == product_id

    async with database() as writer:
        # An uncommitted FK reference holds a KEY SHARE lock on the product.
        # It is invisible to deletion's preflight queries, but blocks deletion.
        await add_history(writer, product_id, kind)
        expected = await snapshot(writer)
        writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        deletion = asyncio.create_task(delete())
        try:
            await asyncio.wait_for(pid_ready.wait(), timeout=5)
            async with database() as observer:
                async with asyncio.timeout(5):
                    while not await observer.scalar(
                        text("SELECT :writer_pid = ANY(pg_blocking_pids(:delete_pid))"),
                        {"writer_pid": writer_pid, "delete_pid": delete_pid},
                    ):
                        if deletion.done():
                            await deletion
                            pytest.fail("Deletion finished without waiting for the pending inventory reference")
                        await asyncio.sleep(0.02)
            await writer.commit()
            await asyncio.wait_for(deletion, timeout=5)
        finally:
            await writer.rollback()
            if not deletion.done():
                deletion.cancel()
            await asyncio.gather(deletion, return_exceptions=True)

    async with database() as session:
        # Includes disposable ProductImage rows: a late FK failure must roll
        # back any earlier ORM cascades along with preserving inventory history.
        assert await snapshot(session) == expected


@pytest.mark.anyio
async def test_unrelated_foreign_key_failure_is_not_reported_as_inventory_history(database):
    product_id = await seed_product(database)
    async with database() as session:
        # This guard lives only in the fixture's disposable schema. It exercises
        # a real 23503 outside the known inventory constraints.
        await session.execute(text(
            "CREATE TABLE product_deletion_guard ("
            "product_id INTEGER NOT NULL, "
            "CONSTRAINT fk_product_deletion_guard_product "
            "FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT)"
        ))
        await session.execute(
            text("INSERT INTO product_deletion_guard (product_id) VALUES (:product_id)"),
            {"product_id": product_id},
        )
        await session.commit()
        before = await snapshot(session)

    async with database() as session:
        product = await session.get(Product, product_id)
        with pytest.raises(IntegrityError) as error:
            await ProductService().delete_product(session, product)
        assert getattr(error.value.orig, "sqlstate", None) == "23503"
        await session.rollback()

    async with database() as session:
        assert await snapshot(session) == before
        assert await session.scalar(text("SELECT count(*) FROM product_deletion_guard")) == 1
