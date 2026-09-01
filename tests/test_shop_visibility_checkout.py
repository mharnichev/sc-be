from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.v1.routes import customers as customer_routes
from app.api.v1.routes import shop_promotions as shop_promotion_routes
from app.models.category import Category
from app.models.product import Product
from app.models.shop import CustomerCartItem, CustomerWishlistItem
from app.schemas.order import OrderCreate
from app.schemas.shop import CartItemCreate, WishlistItemCreate
from app.schemas.shop_promotion import ShopPromotionQuoteRequest
from app.services.catalog_visibility import CatalogVisibility
from app.services.order import OrderService
from app.services.shop_promotion import ShopPriceResult
from app.utils.import_products import apply_product_import_payload, status_to_flags


NOW = datetime(2026, 8, 31, tzinfo=UTC)


def test_imported_out_of_stock_product_remains_visible() -> None:
    assert status_to_flags("Немає в наявності") == ("out_of_stock", True, 0)
    assert status_to_flags(None) == ("unknown", True, 0)


def test_import_update_preserves_manually_hidden_product() -> None:
    hidden_product = product(is_active=False)

    apply_product_import_payload(
        hidden_product,
        {"availability_status": "out_of_stock", "stock_quantity": 0},
    )

    assert hidden_product.is_active is False
    assert hidden_product.availability_status == "out_of_stock"


class ScalarResult:
    def __init__(self, values: list[Any] | None = None, scalar: Any = None) -> None:
        self.values = values or []
        self.scalar = scalar

    def scalars(self) -> ScalarResult:
        return self

    def all(self) -> list[Any]:
        return self.values

    def scalar_one(self) -> Any:
        return self.scalar


class ExecuteSession:
    def __init__(self, results: list[ScalarResult]) -> None:
        self.results = results
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> ScalarResult:
        self.statements.append(statement)
        return self.results.pop(0)


class ProductSession(ExecuteSession):
    def __init__(self, product: Product) -> None:
        super().__init__([])
        self.product = product

    async def get(self, _model: Any, _product_id: int) -> Product:
        return self.product


def product(
    product_id: int = 1,
    *,
    is_active: bool = True,
    stock_quantity: int = 5,
    availability_status: str | None = "in_stock",
    category_id: int | None = None,
) -> Product:
    return Product(
        id=product_id,
        name="Clipper",
        slug=f"clipper-{product_id}",
        price=Decimal("100.00"),
        stock_quantity=stock_quantity,
        is_active=is_active,
        availability_status=availability_status,
        category_id=category_id,
        created_at=NOW,
        updated_at=NOW,
    )


def order_payload(product_id: int = 1, quantity: int = 1) -> OrderCreate:
    return OrderCreate(
        customer_name="Test Customer",
        customer_phone="+380990000001",
        items=[{"product_id": product_id, "quantity": quantity}],
    )


@pytest.mark.anyio
async def test_add_cart_rejects_product_hidden_by_category(monkeypatch: pytest.MonkeyPatch) -> None:
    hidden_category = Category(id=10, name="Hidden", slug="hidden", is_active=False)
    hidden_product = product(category_id=10)
    session = ProductSession(hidden_product)
    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return CatalogVisibility.from_categories([hidden_category])

    monkeypatch.setattr(
        customer_routes.CatalogVisibility,
        "load",
        classmethod(load),
    )

    with pytest.raises(HTTPException) as exc_info:
        await customer_routes.add_cart_item(
            CartItemCreate(product_id=hidden_product.id),
            current_customer=SimpleNamespace(id=7),
            session=session,
        )

    assert exc_info.value.status_code == 404
    assert not session.statements


@pytest.mark.anyio
async def test_add_wishlist_rejects_inactive_product(monkeypatch: pytest.MonkeyPatch) -> None:
    hidden_product = product(is_active=False)
    session = ProductSession(hidden_product)
    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return CatalogVisibility.from_categories([])

    monkeypatch.setattr(
        customer_routes.CatalogVisibility,
        "load",
        classmethod(load),
    )

    with pytest.raises(HTTPException) as exc_info:
        await customer_routes.add_wishlist_item(
            WishlistItemCreate(product_id=hidden_product.id),
            current_customer=SimpleNamespace(id=7),
            session=session,
        )

    assert exc_info.value.status_code == 404
    assert not session.statements


@pytest.mark.anyio
async def test_existing_hidden_cart_item_is_returned_with_unavailable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden_category = Category(id=10, name="Hidden", slug="hidden", is_active=False)
    hidden_product = product(category_id=10)
    item = CustomerCartItem(
        id=5,
        customer_id=7,
        product_id=hidden_product.id,
        quantity=2,
        product=hidden_product,
        created_at=NOW,
        updated_at=NOW,
    )
    session = ExecuteSession([ScalarResult(scalar=1), ScalarResult([item])])
    visibility = CatalogVisibility.from_categories([hidden_category])
    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return visibility

    monkeypatch.setattr(customer_routes.CatalogVisibility, "load", classmethod(load))
    async def no_stats(*_args: Any, **_kwargs: Any) -> dict[int, tuple[None, int]]:
        return {}

    async def prices(_session: Any, products: list[Product], **_kwargs: Any) -> dict[int, ShopPriceResult]:
        return {
            current.id: ShopPriceResult(
                base_price=Decimal(current.price),
                price=Decimal(current.price),
                discount_amount=Decimal("0.00"),
                discount_percent=None,
            )
            for current in products
        }

    monkeypatch.setattr(customer_routes, "_review_stats", no_stats)
    monkeypatch.setattr(customer_routes.shop_promotion_service, "price_products", prices)

    response = await customer_routes.list_cart_items(
        pagination=SimpleNamespace(page=1, page_size=20),
        current_customer=SimpleNamespace(id=7),
        session=session,
    )

    assert response.total == 1
    assert len(response.items) == 1
    assert response.items[0].is_effectively_visible is False
    assert response.items[0].hidden_reason == "category"
    assert response.items[0].is_available_for_purchase is False
    assert response.items[0].product.is_effectively_visible is False
    assert response.items[0].product.hidden_reason == "category"
    assert response.items[0].product.is_available_for_purchase is False


@pytest.mark.anyio
async def test_existing_hidden_wishlist_item_is_returned_with_unavailable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden_category = Category(id=10, name="Hidden", slug="hidden", is_active=False)
    hidden_product = product(category_id=10)
    item = CustomerWishlistItem(
        id=6,
        customer_id=7,
        product_id=hidden_product.id,
        product=hidden_product,
        created_at=NOW,
        updated_at=NOW,
    )
    session = ExecuteSession([ScalarResult(scalar=1), ScalarResult([item])])
    visibility = CatalogVisibility.from_categories([hidden_category])

    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return visibility

    async def no_stats(*_args: Any, **_kwargs: Any) -> dict[int, tuple[None, int]]:
        return {}

    monkeypatch.setattr(customer_routes.CatalogVisibility, "load", classmethod(load))
    monkeypatch.setattr(customer_routes, "_review_stats", no_stats)

    response = await customer_routes.list_wishlist_items(
        pagination=SimpleNamespace(page=1, page_size=20),
        current_customer=SimpleNamespace(id=7),
        session=session,
    )

    assert response.total == 1
    assert len(response.items) == 1
    assert response.items[0].is_effectively_visible is False
    assert response.items[0].hidden_reason == "category"
    assert response.items[0].is_available_for_purchase is False
    assert response.items[0].product.is_effectively_visible is False
    assert response.items[0].product.hidden_reason == "category"
    assert response.items[0].product.is_available_for_purchase is False


@pytest.mark.anyio
async def test_quote_rejects_hidden_product_before_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    hidden_product = product(is_active=False)
    session = ExecuteSession([ScalarResult([hidden_product]), ScalarResult([])])
    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return CatalogVisibility.from_categories([])

    monkeypatch.setattr(
        shop_promotion_routes.CatalogVisibility,
        "load",
        classmethod(load),
    )
    payload = ShopPromotionQuoteRequest(items=[{"productId": hidden_product.id, "quantity": 1}])

    with pytest.raises(HTTPException) as exc_info:
        await shop_promotion_routes.quote_shop_promotion(payload, current_customer=None, session=session)

    assert exc_info.value.status_code == 400
    assert "hidden" in exc_info.value.detail


@pytest.mark.anyio
async def test_order_rejects_hidden_product_before_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    hidden_product = product(is_active=False)
    session = ExecuteSession([ScalarResult([hidden_product]), ScalarResult([])])
    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return CatalogVisibility.from_categories([])

    monkeypatch.setattr(
        "app.services.order.CatalogVisibility.load",
        classmethod(load),
    )

    with pytest.raises(HTTPException) as exc_info:
        await OrderService().create_order(
            session,
            order_payload(hidden_product.id),
            current_customer=SimpleNamespace(id=7),
        )

    assert exc_info.value.status_code == 400
    assert "hidden" in exc_info.value.detail


@pytest.mark.anyio
async def test_order_rejects_visible_but_out_of_stock_product(monkeypatch: pytest.MonkeyPatch) -> None:
    unavailable = product(stock_quantity=0, availability_status="out_of_stock")
    session = ExecuteSession([ScalarResult([unavailable]), ScalarResult([])])
    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return CatalogVisibility.from_categories([])

    monkeypatch.setattr(
        "app.services.order.CatalogVisibility.load",
        classmethod(load),
    )

    with pytest.raises(HTTPException) as exc_info:
        await OrderService().create_order(
            session,
            order_payload(unavailable.id),
            current_customer=SimpleNamespace(id=7),
        )

    assert exc_info.value.status_code == 400
    assert "unavailable" in exc_info.value.detail
