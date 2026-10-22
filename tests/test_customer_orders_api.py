from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.routes import orders
from app.models.order import OrderStatus
from app.schemas.order import OrderCreate
from app.services.order import OrderService
from app.services.catalog_visibility import CatalogVisibility
from app.services.shop_promotion import ShopPriceResult


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.value = scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.value


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def scalar(self, _stmt):
        return self.results.pop(0)

    async def execute(self, stmt):
        self.statements.append(stmt)
        result = self.results.pop(0)
        return result if isinstance(result, Result) else Result(scalar=result)


def order(order_id=1, customer_id=7):
    return SimpleNamespace(
        id=order_id, customer_id=customer_id, created_at=datetime.now(UTC), status=OrderStatus.paid,
        subtotal_amount=Decimal("20.00"), discount_amount=Decimal("2.00"), total_amount=Decimal("18.00"),
        items=[SimpleNamespace(id=3, product_id=5, quantity=2, price=Decimal("9.00"),
            discount_amount=Decimal("1.00"), product_name="Pomade", product_sku="P-1", total_price=Decimal("18.00"))],
        shipping_company="nova_poshta", shipping_method="warehouse", shipping_area="Kyiv",
        shipping_city="Kyiv", shipping_warehouse_number="12", shipping_street=None, building_number=None,
        shipping_apartment=None, delivery_address="Kyiv, 12", payment_method="cashOnDelivery",
        tracking_number="TRACK", external_sync_error="private", external_sync_status="failed",
    )


@pytest.mark.anyio
async def test_order_history_pagination_and_empty_result():
    customer = SimpleNamespace(id=7)
    session = Session([0, Result()])
    response = await orders.list_my_orders(2, 10, customer, session)
    assert response.total == 0 and response.items == []
    assert "orders.customer_id = :customer_id_1" in str(session.statements[0])

    item = order()
    session = Session([1, Result([item])])
    response = await orders.list_my_orders(1, 20, customer, session)
    assert response.total == 1 and response.items[0].item_count == 2
    assert "LIMIT" in str(session.statements[0]) and "OFFSET" in str(session.statements[0])


@pytest.mark.anyio
async def test_order_detail_is_scoped_and_excludes_internal_sync_fields():
    customer = SimpleNamespace(id=7)
    session = Session([Result(scalar=order())])
    response = await orders.get_my_order(1, customer, session)
    assert response.tracking_number == "TRACK"
    assert not hasattr(response, "external_sync_error")
    assert "orders.customer_id = :customer_id_1" in str(session.statements[0])

    session = Session([Result(scalar=None)])
    with pytest.raises(HTTPException) as error:
        await orders.get_my_order(1, customer, session)
    assert error.value.status_code == 404


@pytest.mark.anyio
async def test_guest_order_with_matching_profile_phone_stays_unlinked(monkeypatch):
    product = SimpleNamespace(
        id=5, name="Pomade", sku="P-1", price=Decimal("9.00"), stock_quantity=2,
        is_active=True, availability_status="in_stock", category_id=None,
        image_url="https://images.example/ordered.jpg", package_size="100 ml",
    )

    class ProductResult:
        def scalars(self):
            return self
        def all(self):
            return [product]

    class CheckoutSession:
        def __init__(self):
            self.execute_calls = 0
            self.order = None
        async def execute(self, _statement):
            self.execute_calls += 1
            return ProductResult()
        def add(self, value):
            from app.models.order import Order
            if isinstance(value, Order):
                self.order = value
            value.id = 91
        async def commit(self):
            return None
        async def flush(self):
            for index, item in enumerate(self.order.items, start=1):
                item.id = index
        async def rollback(self):
            return None
        async def refresh(self, _value):
            return None

    async def load(_cls, _session):
        return CatalogVisibility.from_categories([])

    async def price_products(_session, products, **_kwargs):
        return {5: ShopPriceResult(
            base_price=Decimal("9.00"), price=Decimal("9.00"), discount_amount=Decimal("0.00"),
            discount_percent=Decimal("0.00"), promotion_id=None, promotion_name=None, promotion_code=None,
        )}

    monkeypatch.setattr("app.services.order.CatalogVisibility.load", classmethod(load))
    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", price_products)
    session = CheckoutSession()
    created = await OrderService().create_order(session, OrderCreate(
        firstName="Jane", phoneNumber="+380501112233", callbackRequested=True,
        items=[{"productId": 5, "quantity": 1}],
    ))
    assert created.customer_id is None
    assert created.callback_requested is True
    assert created.items[0].product_image_url == "https://images.example/ordered.jpg"
    assert created.items[0].product_variant == "100 ml"
    assert session.execute_calls == 1


def test_customer_order_item_presentation_uses_purchase_snapshot_after_catalog_change():
    from app.schemas.order import CustomerOrderResponse
    purchase = order()
    purchase.callback_requested = False
    purchase.items[0].product_image_url = "https://images.example/ordered.jpg"
    purchase.items[0].product_variant = "100 ml"
    purchase.items[0].product = SimpleNamespace(
        name="Renamed product", price=Decimal("99"), image_url="https://images.example/current.jpg",
        attributes_json={}, images=[], image_variants=[],
    )
    result = CustomerOrderResponse.from_order(purchase)
    assert result.callback_requested is False
    assert result.items[0].product_image_url == "https://images.example/ordered.jpg"
    assert result.items[0].product_variant == "100 ml"
    assert result.items[0].product_name == "Pomade"
    assert result.items[0].price == Decimal("9")
    assert result.items[0].quantity == 2
    purchase.items[0].product.image_url = None
    assert CustomerOrderResponse.from_order(purchase).items[0].product_image_url == "https://images.example/ordered.jpg"


def test_customer_order_image_does_not_lazy_load_unloaded_product():
    from app.models.order import OrderItem
    from app.schemas.order import CustomerOrderResponse
    purchase = order()
    purchase.items = [OrderItem(id=3, product_id=5, quantity=2, price=Decimal("9"),
        discount_amount=Decimal("0"), product_name="Original", product_sku="P-1")]
    assert CustomerOrderResponse.from_order(purchase).items[0].product_image_url is None


def test_callback_preference_defaults_false_and_preserves_legacy_intent_hash():
    from pydantic import ValidationError
    from app.schemas.order import PublicOrderCreate
    from app.services.order import canonical_checkout_payload
    from app.services.order_documents import digest

    raw = {"firstName": "Jane", "phoneNumber": "+380501112233",
        "items": [{"productId": 5, "quantity": 1}]}
    legacy = OrderCreate.model_validate(raw).model_dump(mode="json", by_alias=False)
    legacy.pop("callback_requested")
    omitted = PublicOrderCreate.model_validate(raw)
    refused = PublicOrderCreate.model_validate({**raw, "callbackRequested": False})
    requested = PublicOrderCreate.model_validate({**raw, "callbackRequested": True})
    assert omitted.callback_requested is False
    assert digest(canonical_checkout_payload(omitted)) == digest(legacy)
    assert canonical_checkout_payload(omitted) == canonical_checkout_payload(refused)
    assert digest(canonical_checkout_payload(requested)) != digest(legacy)
    with pytest.raises(ValidationError):
        PublicOrderCreate.model_validate({**raw, "callbackRequested": "yes"})


def test_order_image_snapshot_selects_primary_active_catalog_image():
    from app.services.order import snapshot_image_url

    product = SimpleNamespace(attributes_json={}, image_url="/media/old.webp",
        images=[SimpleNamespace(id=8, sort_order=2, is_active=True, image_url="/media/second.webp"),
            SimpleNamespace(id=7, sort_order=1, is_active=False, image_url="/media/hidden.webp"),
            SimpleNamespace(id=6, sort_order=0, is_active=True, image_url="/media/primary.webp")],
        image_variants=[])
    assert snapshot_image_url(product) == "/media/primary.webp"
    product.images = []
    assert snapshot_image_url(product) == "/media/old.webp"


def test_public_and_admin_order_responses_expose_receipt_snapshots():
    from app.schemas.order import BackofficeOrderResponse, PublicOrderResponse

    raw = {
        "id": 91, "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
        "customer_name": "Jane", "customer_phone": "+380501112233",
        "customer_email": None, "comment": None, "subtotal_amount": "20.00",
        "discount_amount": "2.00", "total_amount": "18.00", "status": "pending",
        "callback_requested": True,
        "items": [{"id": 3, "product_id": 5, "quantity": 2, "price": "9.00",
            "product_name": "Pomade", "product_image_url": "/media/ordered.webp",
            "product_variant": "100 ml", "quantity_from_stock": 2,
            "quantity_to_order": 0, "quantity_received_for_order": 0,
            "procurement_status": "not_required"}],
    }
    for schema in (PublicOrderResponse, BackofficeOrderResponse):
        response = schema.model_validate(raw).model_dump(mode="json")
        assert response["callback_requested"] is True
        assert response["items"][0]["product_image_url"] == "/media/ordered.webp"
        assert response["items"][0]["product_variant"] == "100 ml"
    historical = PublicOrderResponse.model_validate({**raw, "callback_requested": None,
        "items": [{**raw["items"][0], "product_image_url": None, "product_variant": None}]})
    assert historical.callback_requested is None
    assert historical.items[0].product_image_url is None
