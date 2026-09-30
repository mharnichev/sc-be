from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import CheckConstraint

from app.models.inventory import InventoryCount, InventoryDocumentStatus, InventoryMovementType, InventoryReceipt
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
from app.models.product import Product
from app.schemas.order import OrderCreate
from app.schemas.order import CustomerOrderItemResponse
from app.schemas.product import ShopProductResponse
from app.services.inventory import InventoryService, normalize_barcode
from app.services.order import OrderService
from app.api.v1.routes.inventory import IdempotencyKey


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class SettingsSession:
    """Enough of AsyncSession to exercise barcode locking/duplicate handling."""

    def __init__(self, product: Product, duplicate_id: int | None = None):
        self.product = product
        self.duplicate_id = duplicate_id
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return ScalarResult(self.product)
        return ScalarResult(self.duplicate_id)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _value):
        return None


class OrderResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values

    def __iter__(self):
        return iter(self.values)

    def scalar_one_or_none(self):
        return self.values


class OrderSession:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return OrderResult(self.results.pop(0))

    def add(self, value):
        self.added.append(value)
        if isinstance(value, Order):
            value.id = 71
            for position, item in enumerate(value.items, start=1):
                item.id = position

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    async def refresh(self, _value):
        return None


def product(*, barcode: str | None = None) -> Product:
    return Product(
        id=17,
        name="Pomade",
        slug="pomade",
        price=100,
        stock_quantity=8,
        reserved_quantity=3,
        barcode=barcode,
        allow_backorder=False,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("   \t", None),
        ("  ab  12\n34 ", "AB1234"),
        ("ean-13", "EAN-13"),
    ],
)
def test_normalize_barcode_removes_all_whitespace_and_uppercases(raw, expected):
    assert normalize_barcode(raw) == expected


def test_normalize_barcode_rejects_values_too_long_for_storage():
    with pytest.raises(HTTPException) as error:
        normalize_barcode("x" * 65)
    assert error.value.status_code == 422


@pytest.mark.anyio
async def test_barcode_settings_are_normalized_under_lock_and_duplicate_is_conflict():
    item = product()
    session = SettingsSession(item)

    updated = await InventoryService().update_product_settings(
        session, item.id, barcode=" ab 12 ", allow_backorder=True
    )

    assert updated is item
    assert (item.barcode, item.allow_backorder, session.commits) == ("AB12", True, 1)
    assert "FOR UPDATE" in str(session.statements[0])

    duplicate_session = SettingsSession(product(), duplicate_id=999)
    with pytest.raises(HTTPException) as error:
        await InventoryService().update_product_settings(
            duplicate_session, 17, barcode="AB12", allow_backorder=False
        )
    assert error.value.status_code == 409
    assert duplicate_session.commits == 0


def test_product_inventory_constraints_preserve_nonnegative_available_stock():
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in Product.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert next(value for name, value in constraints.items() if name.endswith("stock_quantity_nonnegative")) == "stock_quantity >= 0"
    assert next(value for name, value in constraints.items() if name.endswith("reserved_quantity_nonnegative")) == "reserved_quantity >= 0"
    assert next(value for name, value in constraints.items() if name.endswith("available_quantity_nonnegative")) == "stock_quantity - reserved_quantity >= 0"


def test_ledger_movement_captures_every_stock_or_reservation_delta():
    item = product()
    movement = InventoryService._movement(
        product=item,
        movement_type=InventoryMovementType.reservation,
        quantity=4,
        on_hand_delta=0,
        reserved_delta=4,
        reason="Order reservation",
        order_id=4,
        order_item_id=9,
        reference_key="order:4:item:9:reserve",
    )

    assert movement.product_id == item.id
    assert movement.movement_type == InventoryMovementType.reservation
    assert (movement.quantity, movement.on_hand_delta, movement.reserved_delta) == (4, 0, 4)
    assert (movement.order_id, movement.order_item_id) == (4, 9)
    assert movement.reference_key == "order:4:item:9:reserve"


def test_terminal_inventory_documents_are_explicitly_representable_for_idempotent_posts():
    # Posted is a durable document state: posting code can recognize a replay
    # before changing balances or appending another movement.
    assert InventoryDocumentStatus.posted.value == "posted"


@pytest.mark.anyio
async def test_reposting_terminal_receipt_or_count_is_a_noop():
    receipt = InventoryReceipt(id=8, status=InventoryDocumentStatus.posted, reason="Supplier delivery")
    receipt_session = OrderSession([receipt])
    assert await InventoryService().post_receipt(receipt_session, receipt.id, admin_user_id=1, idempotency_key="again") is receipt
    assert receipt_session.commits == 0

    count = InventoryCount(id=9, status=InventoryDocumentStatus.posted, reason="Cycle count")
    count_session = OrderSession([count])
    assert await InventoryService().post_count(count_session, count.id, admin_user_id=1, idempotency_key="again") is count
    assert count_session.commits == 0


@pytest.mark.anyio
async def test_manual_operation_idempotency_replay_returns_the_original_movement():
    movement = InventoryService._movement(
        product=product(),
        movement_type=InventoryMovementType.receipt,
        quantity=2,
        on_hand_delta=2,
        reserved_delta=0,
        reason="Supplier delivery",
        reference_key="manual-receipt-1",
    )
    session = OrderSession([movement])

    replayed = await InventoryService().manual_operation(
        session,
        movement_type=InventoryMovementType.receipt,
        product_id=17,
        quantity=2,
        reason="Supplier delivery",
        admin_user_id=1,
        idempotency_key="manual-receipt-1",
    )

    assert replayed is movement
    assert session.commits == 0
    assert session.added == []


def test_idempotency_header_matches_database_key_length():
    header = IdempotencyKey.__metadata__[0]
    max_length = next(metadata.max_length for metadata in header.metadata if hasattr(metadata, "max_length"))
    assert max_length == 128


def test_customer_facing_schemas_do_not_leak_inventory_or_procurement_fields():
    forbidden = {
        "reserved_quantity",
        "available",
        "allow_backorder",
        "quantity_from_stock",
        "quantity_to_order",
        "quantity_received_for_order",
        "procurement_status",
        "purchase_unit_cost",
        "inventory_movements",
    }
    # ProductResponse/OrderItemResponse are staff-facing backoffice payloads.
    # The public shop/customer payloads must remain safe to expose.
    for schema in (ShopProductResponse, CustomerOrderItemResponse):
        assert not (forbidden & set(schema.model_fields)), schema.__name__


def checkout_payload(quantity: int = 4) -> OrderCreate:
    return OrderCreate(firstName="Jane", phoneNumber="+380501112233", items=[{"productId": 17, "quantity": quantity}])


@pytest.mark.anyio
async def test_checkout_allocates_available_stock_and_records_reservation_ledger(monkeypatch):
    item = product()
    item.allow_backorder = True
    session = OrderSession([[item]])

    async def visibility(_cls, _session):
        return SimpleNamespace(
            product_states=lambda products: {row.id: SimpleNamespace(is_effectively_visible=True) for row in products},
            category_parents=lambda: {},
        )

    async def prices(_session, products, **_kwargs):
        return {row.id: SimpleNamespace(base_price=100, price=90, discount_amount=10,
                                         promotion_id=None, promotion_name=None, promotion_code=None) for row in products}

    monkeypatch.setattr("app.services.order.CatalogVisibility.load", classmethod(visibility))
    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", prices)
    order = await OrderService().create_order(session, checkout_payload())

    line = order.items[0]
    assert (line.quantity_from_stock, line.quantity_to_order, line.procurement_status) == (
        4, 0, ProcurementStatus.not_required,
    )
    assert item.reserved_quantity == 7
    movement = next(row for row in session.added if getattr(row, "movement_type", None))
    assert (movement.on_hand_delta, movement.reserved_delta, movement.quantity) == (0, 4, 4)
    assert "FOR UPDATE" in str(session.statements[0])


@pytest.mark.anyio
async def test_checkout_splits_backorder_shortage_and_rejects_it_when_disabled(monkeypatch):
    item = product()
    item.stock_quantity, item.reserved_quantity, item.allow_backorder = 5, 3, True
    session = OrderSession([[item]])

    async def visibility(_cls, _session):
        return SimpleNamespace(
            product_states=lambda products: {row.id: SimpleNamespace(is_effectively_visible=True) for row in products},
            category_parents=lambda: {},
        )

    async def prices(_session, products, **_kwargs):
        return {row.id: SimpleNamespace(base_price=100, price=100, discount_amount=0,
                                         promotion_id=None, promotion_name=None, promotion_code=None) for row in products}

    monkeypatch.setattr("app.services.order.CatalogVisibility.load", classmethod(visibility))
    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", prices)
    order = await OrderService().create_order(session, checkout_payload())
    assert (order.items[0].quantity_from_stock, order.items[0].quantity_to_order) == (2, 2)
    assert order.items[0].procurement_status == ProcurementStatus.to_order

    disabled = product()
    disabled.stock_quantity, disabled.reserved_quantity, disabled.allow_backorder = 5, 3, False
    with pytest.raises(HTTPException) as error:
        await OrderService().create_order(OrderSession([[disabled]]), checkout_payload())
    assert error.value.status_code == 409

    fully_short = product()
    fully_short.stock_quantity = fully_short.reserved_quantity = 3
    fully_short.allow_backorder = True
    backordered = await OrderService().create_order(OrderSession([[fully_short]]), checkout_payload())
    assert (backordered.items[0].quantity_from_stock, backordered.items[0].quantity_to_order) == (0, 4)
    assert backordered.items[0].procurement_status == ProcurementStatus.to_order


@pytest.mark.anyio
async def test_cancel_releases_all_reserved_quantity_and_repeated_terminal_status_is_a_noop():
    item = product()
    item.reserved_quantity = 5
    order_item = OrderItem(id=3, product_id=item.id, quantity=5, quantity_from_stock=2,
                           quantity_to_order=3, quantity_received_for_order=3,
                           procurement_status=ProcurementStatus.received, price=100)
    order = Order(id=9, customer_name="Jane", customer_phone="+380501112233", total_amount=500,
                  status=OrderStatus.paid, items=[order_item])
    session = OrderSession([[order_item], order, [item]])
    result = await OrderService().update_status(session, order.id, OrderStatus.cancelled, admin_user_id=1)
    assert result.status == OrderStatus.cancelled
    assert item.reserved_quantity == 0
    release = next(row for row in session.added if getattr(row, "movement_type", None))
    assert (release.movement_type, release.reserved_delta, release.quantity) == (
        InventoryMovementType.reservation_release, -5, 5,
    )

    replay = OrderSession([[order_item], order])
    assert await OrderService().update_status(replay, order.id, OrderStatus.cancelled, admin_user_id=1) is order
    assert replay.commits == 0


@pytest.mark.anyio
async def test_complete_order_consumes_reserved_stock_and_records_shipment_once():
    item = product()
    item.stock_quantity = item.reserved_quantity = 5
    order_item = OrderItem(
        id=3,
        product_id=item.id,
        quantity=5,
        quantity_from_stock=2,
        quantity_to_order=3,
        quantity_received_for_order=3,
        procurement_status=ProcurementStatus.received,
        price=100,
    )
    order = Order(
        id=9,
        customer_name="Jane",
        customer_phone="+380501112233",
        total_amount=500,
        status=OrderStatus.paid,
        items=[order_item],
    )
    session = OrderSession([[order_item], order, [item]])

    result = await OrderService().update_status(
        session,
        order.id,
        OrderStatus.completed,
        admin_user_id=1,
    )

    assert result.status == OrderStatus.completed
    assert (item.stock_quantity, item.reserved_quantity) == (0, 0)
    shipment = next(row for row in session.added if getattr(row, "movement_type", None))
    assert (shipment.movement_type, shipment.on_hand_delta, shipment.reserved_delta, shipment.quantity) == (
        InventoryMovementType.shipment,
        -5,
        -5,
        5,
    )

    replay = OrderSession([[order_item], order])
    assert await OrderService().update_status(
        replay,
        order.id,
        OrderStatus.completed,
        admin_user_id=1,
    ) is order
    assert replay.commits == 0
