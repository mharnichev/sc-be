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
        firstName="Jane", phoneNumber="+380501112233", items=[{"productId": 5, "quantity": 1}],
    ))
    assert created.customer_id is None
    assert session.execute_calls == 1
