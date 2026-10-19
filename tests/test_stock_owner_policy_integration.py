"""Owner-approved stock contracts against the guarded isolated PostgreSQL fixture."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from test_inventory_integration import anyio_backend, database, checkout_payload  # noqa: F401
from app.core.config import settings
from app.models.inventory import InventoryMovement, InventoryReceiptItemRequest
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
from app.models.product import Product
from app.models.shop import ProductView
from app.services.catalog_visibility import CatalogVisibility
from app.services.inventory import InventoryService
from app.services.order import OrderService
from app.services.product_popularity import ProductPopularityService
from app.utils import import_products as importer


async def seed_product(database, *, stock=0, reserved=0, active=True):
    async with database() as session:
        product = Product(name="Test pomade", slug="test-pomade", sku="TEST-1", price=100,
                          stock_quantity=stock, reserved_quantity=reserved, is_active=active,
                          availability_status="out_of_stock", allow_backorder=False)
        session.add(product)
        await session.commit()
        return product.id


@pytest.mark.anyio
async def test_supplier_import_preserves_local_balances_and_new_products_start_empty(database, monkeypatch):
    product_id = await seed_product(database, stock=7, reserved=3, active=False)
    rows = [{"Артикул": "TEST-1", "Название (UA)": "Test pomade", "Цена": "100", "Наличие": "В наявності"},
            {"Артикул": "TEST-NEW", "Название (UA)": "New pomade", "Цена": "100", "Наличие": "В наявності"}]
    monkeypatch.setattr(importer, "AsyncSessionLocal", database)
    monkeypatch.setattr(importer, "parse_xlsx", lambda _path: rows)
    await importer.import_products(Path("synthetic-test.xlsx"))
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity, product.is_active) == (7, 3, False)
        assert product.availability_status == "in_stock"
        new = (await session.execute(select(Product).where(Product.sku == "TEST-NEW"))).scalar_one()
        assert (new.stock_quantity, new.reserved_quantity, new.available_quantity) == (0, 0, 0)
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 0
    rows[0]["Наличие"] = "Немає в наявності"
    await importer.import_products(Path("synthetic-test.xlsx"))
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity, product.is_active) == (7, 3, False)
        assert product.availability_status == "out_of_stock"
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 0


@pytest.mark.anyio
async def test_local_free_stock_remains_purchasable_and_paid_reserve_survives_until_shipment(database):
    product_id = await seed_product(database, stock=2)
    async with database() as session:
        product = await session.get(Product, product_id)
        assert CatalogVisibility.from_categories([]).is_available_for_purchase(product)
        order = await OrderService().create_order(session, checkout_payload(product_id))
        order_id = order.id
        # Exercise the historical status compatibility path; new orders use profile commands.
        order.channel = None
        await session.commit()
    async with database() as session:
        await OrderService().update_status(session, order_id, OrderStatus.paid, admin_user_id=1)
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity, product.available_quantity) == (2, 1, 1)
    async with database() as session:
        await OrderService().update_status(session, order_id, OrderStatus.completed, admin_user_id=1)
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity, product.available_quantity) == (1, 0, 1)


@pytest.mark.anyio
@pytest.mark.parametrize("active, backorder, expected_error", [(True, False, 409), (True, True, None), (False, True, 400)])
async def test_supplier_state_does_not_bypass_backorder_or_visibility_rules(database, active, backorder, expected_error):
    product_id = await seed_product(database, active=active)
    async with database() as session:
        product = await session.get(Product, product_id)
        product.allow_backorder = backorder
        product.availability_status = "in_stock"
        await session.commit()
        assert CatalogVisibility.from_categories([]).is_available_for_purchase(product) == (expected_error is None)
        if expected_error is None:
            order = await OrderService().create_order(session, checkout_payload(product_id))
            item = (await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalar_one()
            assert (item.quantity_from_stock, item.quantity_to_order) == (0, 1)
        else:
            with pytest.raises(HTTPException) as error:
                await OrderService().create_order(session, checkout_payload(product_id))
            assert error.value.status_code == expected_error
            await session.rollback()
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 0


@pytest.mark.anyio
async def test_anonymous_quantity_limit_still_applies_with_sufficient_local_stock(database):
    product_id = await seed_product(database, stock=20)
    payload = checkout_payload(product_id)
    payload.items[0].quantity = 11
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await OrderService().create_order(session, payload)
        assert error.value.status_code == 400
        assert "10 units" in error.value.detail
        await session.rollback()
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (20, 0)
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", [OrderStatus.cancelled, OrderStatus.completed])
async def test_legacy_order_cannot_take_new_orders_reservation(database, terminal):
    product_id = await seed_product(database, stock=1)
    async with database() as session:
        new_order = await OrderService().create_order(session, checkout_payload(product_id))
        new_id = new_order.id
        # Owned reservation fixture on the historical status compatibility path.
        new_order.channel = None
        legacy = Order(customer_name="Historical", customer_phone="+380501112233", total_amount=100,
                       status=OrderStatus.paid, items=[OrderItem(product_id=product_id, quantity=1, price=100,
                       quantity_from_stock=1, quantity_to_order=0, quantity_received_for_order=0,
                       procurement_status=ProcurementStatus.not_required)])
        session.add(legacy)
        await session.commit()
        legacy_id = legacy.id
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await OrderService().update_status(session, legacy_id, terminal, admin_user_id=1)
        assert error.value.status_code == 409
        assert "ownership" in error.value.detail
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (1, 1)
        assert (await session.get(Order, legacy_id)).status == OrderStatus.paid
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
        await OrderService().update_status(session, new_id, terminal, admin_user_id=1)
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (
            (1, 0) if terminal == OrderStatus.cancelled else (0, 0)
        )


async def add_scan(session, receipt_id, product_id, key, **overrides):
    payload = dict(product_id=product_id, barcode=None, quantity=2, purchase_unit_cost=Decimal("50.00"),
                   batch_number="LOT-A", expiration_date=date(2027, 1, 1), allocations=[], idempotency_key=key)
    payload.update(overrides)
    return await InventoryService().add_receipt_item(session, receipt_id, **payload)


@pytest.mark.anyio
async def test_receipt_scan_retry_preserves_quantity_allocations_and_replays_after_post(database):
    product_id = await seed_product(database)
    service = InventoryService()
    async with database() as session:
        order = Order(customer_name="Procurement", customer_phone="+380501112233", total_amount=400,
                      items=[OrderItem(product_id=product_id, quantity=4, price=100, quantity_from_stock=0,
                      quantity_to_order=4, quantity_received_for_order=0, procurement_status=ProcurementStatus.ordered)])
        session.add(order)
        await session.commit()
        line_id = order.items[0].id
        receipt = await service.create_receipt(session, reason="Retry", comment=None, idempotency_key="create")
        receipt_id = receipt.id
        allocations = [{"order_item_id": line_id, "quantity": 2}]
        first = await add_scan(session, receipt_id, product_id, "scan-1", allocations=allocations)
        line_id_receipt = first.items[0].id
        replay = await add_scan(session, receipt_id, product_id, "scan-1", allocations=allocations)
        assert replay.items[0].id == line_id_receipt
        assert (replay.items[0].quantity, replay.items[0].allocations[0].quantity) == (2, 2)
        second = await add_scan(session, receipt_id, product_id, "scan-2", allocations=allocations)
        assert len(second.items) == 1
        assert (second.items[0].quantity, second.items[0].allocations[0].quantity) == (4, 4)
        await service.post_receipt(session, receipt_id, admin_user_id=1, idempotency_key="post")
        replay = await add_scan(session, receipt_id, product_id, "scan-1", allocations=allocations)
        assert replay.status.value == "posted"
        assert replay.items[0].quantity == 4
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (4, 4)
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptItemRequest)) == 2
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 2
        with pytest.raises(HTTPException) as error:
            await add_scan(session, receipt_id, product_id, "new-after-post")
        assert error.value.status_code == 409


@pytest.mark.anyio
@pytest.mark.parametrize("changed", [dict(quantity=3), dict(purchase_unit_cost=Decimal("51.00")),
                                     dict(batch_number="LOT-B"), dict(expiration_date=date(2027, 2, 1)),
                                     dict(allocations=[{"order_item_id": 999, "quantity": 1}])])
async def test_receipt_scan_same_key_changed_payload_is_409_and_atomic(database, changed):
    product_id = await seed_product(database)
    async with database() as session:
        receipt = await InventoryService().create_receipt(session, reason="Retry", comment=None, idempotency_key="create")
        receipt_id = receipt.id
        await add_scan(session, receipt_id, product_id, "scan-1")
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await add_scan(session, receipt_id, product_id, "scan-1", **changed)
        assert error.value.status_code == 409
    async with database() as session:
        receipt = await InventoryService().get_receipt(session, receipt_id)
        assert len(receipt.items) == 1
        assert (receipt.items[0].quantity, receipt.items[0].purchase_unit_cost, receipt.items[0].batch_number) == (2, Decimal("50.00"), "LOT-A")
        assert receipt.items[0].allocations == []
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptItemRequest)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("keys, expected", [(('scan-1', 'scan-1'), 2), (('scan-1', 'scan-2'), 4)])
async def test_concurrent_same_request_replays_and_distinct_scans_add(database, keys, expected):
    product_id = await seed_product(database)
    async with database() as session:
        receipt = await InventoryService().create_receipt(session, reason="Concurrency", comment=None, idempotency_key="create")
        receipt_id = receipt.id
    async def attempt(key):
        async with database() as session:
            return await add_scan(session, receipt_id, product_id, key)
    await asyncio.wait_for(asyncio.gather(*(attempt(key) for key in keys)), timeout=5)
    async with database() as session:
        receipt = await InventoryService().get_receipt(session, receipt_id)
        assert len(receipt.items) == 1
        assert receipt.items[0].quantity == expected
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptItemRequest)) == len(set(keys))


@pytest.mark.anyio
async def test_distinct_cost_or_lot_scans_remain_separate(database):
    product_id = await seed_product(database)
    async with database() as session:
        receipt = await InventoryService().create_receipt(session, reason="Lots", comment=None, idempotency_key="create")
        await add_scan(session, receipt.id, product_id, "scan-a")
        await add_scan(session, receipt.id, product_id, "scan-b", batch_number="LOT-B")
        receipt = await add_scan(session, receipt.id, product_id, "scan-c", purchase_unit_cost=Decimal("51.00"))
        assert sorted((item.batch_number, item.purchase_unit_cost, item.quantity) for item in receipt.items) == [
            ("LOT-A", Decimal("50.00"), 2), ("LOT-A", Decimal("51.00"), 2), ("LOT-B", Decimal("50.00"), 2)]


@pytest.mark.anyio
async def test_cached_receipt_session_refreshes_concurrent_quantity_and_allocations(database):
    product_id = await seed_product(database)
    async with database() as first:
        order = Order(customer_name="Procurement", customer_phone="+380501112233", total_amount=300,
                      items=[OrderItem(product_id=product_id, quantity=3, price=100, quantity_from_stock=0,
                      quantity_to_order=3, quantity_received_for_order=0, procurement_status=ProcurementStatus.ordered)])
        first.add(order)
        await first.commit()
        allocations = [{"order_item_id": order.items[0].id, "quantity": 1}]
        receipt = await InventoryService().create_receipt(first, reason="Cached session", comment=None, idempotency_key="create")
        receipt_id = receipt.id
        cached = await add_scan(first, receipt_id, product_id, "scan-a", allocations=allocations)
        assert (cached.items[0].quantity, cached.items[0].allocations[0].quantity) == (2, 1)
        async with database() as second:
            await add_scan(second, receipt_id, product_id, "scan-b", allocations=allocations)
        final = await add_scan(first, receipt_id, product_id, "scan-c", allocations=allocations)
        assert len(final.items) == 1
        assert (final.items[0].quantity, final.items[0].allocations[0].quantity) == (6, 3)


@pytest.mark.anyio
async def test_popularity_uses_free_stock_and_exact_kyiv_calendar_dates(database, monkeypatch):
    now = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)  # Kyiv October 10
    monkeypatch.setattr(settings, "product_top_window_days", 3)
    monkeypatch.setattr(settings, "product_top_fraction", 1.0)
    monkeypatch.setattr(settings, "product_top_min_unique_views", 1)
    monkeypatch.setattr(settings, "product_top_min_paid_orders", 1)
    product_id = await seed_product(database, stock=2, reserved=1)
    async with database() as session:
        reserved = Product(name="Reserved", slug="reserved", price=100, stock_quantity=2, reserved_quantity=2)
        hidden = Product(name="Hidden", slug="hidden", price=100, stock_quantity=2, reserved_quantity=0, is_active=False)
        session.add_all([reserved, hidden])
        await session.flush()
        for product in [await session.get(Product, product_id), reserved, hidden]:
            for day in [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)]:
                session.add(ProductView(product_id=product.id, visitor_hash=f"visitor-{day}", viewed_on=day))
        boundary = datetime(2026, 10, 7, 21, tzinfo=UTC)  # First included Kyiv midnight
        for created in [boundary - timedelta(seconds=1), boundary, now, now + timedelta(seconds=1)]:
            session.add(Order(customer_name="Paid", customer_phone="+380501112233", total_amount=100,
                              status=OrderStatus.paid, created_at=created,
                              items=[OrderItem(product_id=product_id, quantity=1, price=100,
                                               quantity_from_stock=0, quantity_to_order=1,
                                               quantity_received_for_order=0,
                                               procurement_status=ProcurementStatus.to_order)]))
        await session.commit()
        await ProductPopularityService().recalculate(session, now=now)
        await session.commit()
        await session.refresh(reserved)
        await session.refresh(hidden)
        product = await session.get(Product, product_id, populate_existing=True)
        assert (product.is_top, product.top_unique_views_30d, product.top_paid_orders_30d, product.top_purchased_units_30d) == (True, 3, 2, 2)
        assert (reserved.is_top, reserved.top_score, reserved.top_rank) == (False, Decimal("0"), None)
        assert hidden.is_top is False


@pytest.mark.anyio
async def test_view_deduplication_uses_kyiv_day_across_utc_midnight(database):
    product_id = await seed_product(database, stock=1)
    service = ProductPopularityService()
    async with database() as session:
        first = await service.record_view(session, product_id=product_id, visitor_hash="visitor", now=datetime(2026, 10, 9, 21, 30, tzinfo=UTC))
        same = await service.record_view(session, product_id=product_id, visitor_hash="visitor", now=datetime(2026, 10, 10, 0, 30, tzinfo=UTC))
        following = await service.record_view(session, product_id=product_id, visitor_hash="visitor", now=datetime(2026, 10, 10, 21, 30, tzinfo=UTC))
        assert (first.recorded, first.viewed_on) == (True, date(2026, 10, 10))
        assert (same.recorded, same.viewed_on) == (False, date(2026, 10, 10))
        assert (following.recorded, following.viewed_on) == (True, date(2026, 10, 11))
