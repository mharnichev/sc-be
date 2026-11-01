from datetime import UTC, datetime
from decimal import Decimal
import importlib.util
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.models.product import Product
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
from app.schemas.product import ProductCreate, ProductResponse, ProductUpdate
from app.services.catalog_assembly import build_shop_product_response, product_order_clauses, volume_variant_responses
from app.services.catalog_visibility import CatalogVisibility, VisibilityState
from app.services.product import ProductService
from app.services.shop_promotion import ShopPromotionService
from app.utils.import_products import apply_product_import_payload


@pytest.fixture
def anyio_backend():
    return "asyncio"


def product(**changes):
    now = datetime.now(UTC)
    values = dict(id=1, name="Shampoo", slug="shampoo", price=Decimal("100"),
                  recommended_retail_price=Decimal("200"), shop_price=Decimal("180"),
                  volume_ml=100, stock_quantity=2, reserved_quantity=0, is_active=True,
                  created_at=now, updated_at=now)
    values.update(changes)
    return Product(**values)


@pytest.mark.parametrize("shop_price,expected", [(None, "100"), (Decimal("0"), "0"), (Decimal("180"), "180")])
def test_fallback_and_serialization(shop_price, expected):
    item = product(shop_price=shop_price)
    pricing = ShopPromotionService.calculate_product_price(item, [], category_parents={})
    response = build_shop_product_response(item, categories={}, pricing=pricing,
        visibility_state=VisibilityState(True, None), is_available_for_purchase=True)
    fallback = build_shop_product_response(item, categories={},
        visibility_state=VisibilityState(True, None), is_available_for_purchase=True)
    variants = volume_variant_responses([item], {1: pricing}, CatalogVisibility.from_categories([]))
    private = ProductResponse.model_validate(item)
    assert response.price == fallback.price == variants[0].price == Decimal(expected)
    assert response.base_price == variants[0].base_price == Decimal(expected)
    assert response.shop_price == private.shop_price == shop_price
    assert private.price == item.price == Decimal("100")
    assert response.recommended_retail_price == Decimal("200")


def test_promotions_discount_shop_price_not_supplier_price():
    promo = ShopPromotion(id=1, name="Sale", trigger=ShopPromotionTrigger.automatic,
        discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal("10"),
        priority=100, applies_to_all_products=True, include_subcategories=True,
        is_active=True, products=[], categories=[], brands=[])
    pricing = ShopPromotionService.calculate_product_price(product(), [promo], category_parents={})
    assert pricing.base_price == Decimal("180")
    assert pricing.price == Decimal("162")


@pytest.mark.anyio
@pytest.mark.parametrize("shop_price,expected", [(Decimal("180"), Decimal("180")), (None, Decimal("100")), (Decimal("0"), Decimal("0"))])
@pytest.mark.parametrize("collection", ["cart", "wishlist"])
async def test_hidden_customer_collection_uses_shop_price(monkeypatch, shop_price, expected, collection):
    from types import SimpleNamespace
    from app.api.v1.routes import customers

    item = product(shop_price=shop_price, is_active=False)
    visibility = CatalogVisibility.from_categories([])
    price_products = AsyncMock()
    monkeypatch.setattr(customers.shop_promotion_service, "price_products", price_products)
    prices = await customers._customer_item_prices(AsyncMock(), [item], visibility)
    saved = SimpleNamespace(id=1, product_id=item.id, product=item, quantity=2,
        created_at=item.created_at, updated_at=item.updated_at)
    build_response = getattr(customers, f"_{collection}_item_response")
    response = build_response(saved, categories={}, stats={}, visibility=visibility, pricing=prices[item.id])

    price_products.assert_not_awaited()
    assert response.is_effectively_visible is False
    assert response.product.price == response.product.base_price == expected
    assert response.product.shop_price == shop_price
    assert item.price == Decimal("100")


@pytest.mark.anyio
@pytest.mark.parametrize("shop_price,accepted", [("180", True), ("120", False)])
async def test_fixed_promo_code_eligibility_uses_shop_price(shop_price, accepted):
    service = ShopPromotionService()
    promo = ShopPromotion(id=1, name="Code", code="SALE", trigger=ShopPromotionTrigger.promocode,
        discount_type=ShopPromotionDiscountType.fixed_price, discount_value=Decimal("150"),
        priority=100, applies_to_all_products=True, include_subcategories=True,
        is_active=True, products=[], categories=[], brands=[])
    service._automatic_promotions = AsyncMock(return_value=[])
    service._promotion_by_code = AsyncMock(return_value=promo)
    item = product(shop_price=Decimal(shop_price))
    if accepted:
        prices = await service.price_products(AsyncMock(), [item], category_parents={}, promo_code="SALE")
        assert prices[1].price == Decimal("150")
    else:
        with pytest.raises(HTTPException, match="Promo code does not apply"):
            await service.price_products(AsyncMock(), [item], category_parents={}, promo_code="SALE")


@pytest.mark.parametrize("value", ["0", "-1", "1.001", "100000000.00"])
@pytest.mark.parametrize("schema", [ProductCreate, ProductUpdate])
def test_api_rejects_invalid_manual_money(value, schema):
    with pytest.raises(ValidationError):
        schema(name="Shampoo", slug="shampoo", price="100", shop_price=value)


@pytest.mark.anyio
@pytest.mark.parametrize("rrp,explicit,expected", [(None, None, "100"), ("200", None, "200"), ("200", "180", "180")])
async def test_create_default_and_update_independence(rrp, explicit, expected):
    service = ProductService()
    service._validate_brand_and_category = AsyncMock()
    service._validate_uniqueness = AsyncMock()
    session = AsyncMock()
    session.add = MagicMock()
    data = dict(name="Shampoo", slug="shampoo", price=Decimal("100"),
        recommended_retail_price=Decimal(rrp) if rrp else None,
        shop_price=Decimal(explicit) if explicit else None)
    item = await service.create_product(session, data)
    assert item.shop_price == Decimal(expected)
    assert data["shop_price"] == (Decimal(explicit) if explicit else None)
    await service.update_product(session, item, {"price": Decimal("110"), "recommended_retail_price": Decimal("220")})
    apply_product_import_payload(item, {"price": Decimal("120"), "recommended_retail_price": Decimal("230"), "shop_price": Decimal("999")})
    assert item.shop_price == Decimal(expected)
    assert item.price == Decimal("120")


@pytest.mark.parametrize("sort", ["price_asc", "price", "cheap", "cheaper", "price_desc", "-price", "expensive"])
def test_sql_sort_uses_shop_price_with_legacy_fallback(sort):
    sql = str(select(Product.id).order_by(*product_order_clauses(sort, None)))
    assert "coalesce(products.shop_price, products.price)" in sql
    assert sql.index("stock_quantity") < sql.index("coalesce(products.shop_price")


@pytest.mark.anyio
async def test_category_filter_sort_and_range_use_shop_prices(monkeypatch):
    from app.api.v1.routes import categories
    from app.dependencies.common import PaginationParams

    items = [product(id=1, shop_price=Decimal("180")), product(id=2, price=Decimal("300"), shop_price=Decimal("150"))]
    visibility = CatalogVisibility.from_categories([])
    monkeypatch.setattr(categories, "_category_products_stmt", AsyncMock(return_value=(select(Product), [], visibility)))
    monkeypatch.setattr(categories, "_review_stats", AsyncMock(return_value={}))
    prices = {item.id: ShopPromotionService.calculate_product_price(item, [], category_parents={}) for item in items}
    monkeypatch.setattr(categories.shop_promotion_service, "price_products", AsyncMock(return_value=prices))
    session = AsyncMock()
    session.execute.return_value = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = items
    request = Request({"type": "http", "query_string": b""})
    result = await categories.list_category_products("all", request, PaginationParams(),
        sort="price_asc", ordering=None, price_min=Decimal("140"), price_max=Decimal("190"),
        price_min_snake=None, price_max_snake=None, has_discount=None, is_top=None, limit=None, offset=None, session=session)
    assert [item.id for item in result.items] == [2, 1]
    result = await categories.list_category_products("all", request, PaginationParams(),
        sort="price_desc", ordering=None, price_min=Decimal("160"), price_max=Decimal("190"),
        price_min_snake=None, price_max_snake=None, has_discount=None, is_top=None, limit=None, offset=None, session=session)
    assert [item.id for item in result.items] == [1]
    facets = await categories.category_filters("all", has_discount=None, is_top=None, session=session)
    assert (facets.price.min, facets.price.max) == (Decimal("150"), Decimal("180"))


def test_approved_migration_uses_rrp_with_price_fallback_and_preserves_sources(monkeypatch):
    path = Path(__file__).parents[1] / "alembic/versions/0141_product_shop_price.py"
    spec = importlib.util.spec_from_file_location("shop_price_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = MagicMock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert migration.down_revision == "0140_shop_order_receipt_snapshot"
    operations.execute.assert_called_once_with("UPDATE products SET shop_price = COALESCE(recommended_retail_price, price)")
    operations.create_check_constraint.assert_called_once_with("products_shop_price_nonnegative", "products", "shop_price >= 0")
    assert any(c.name == "ck_products_products_shop_price_nonnegative" for c in Product.__table__.constraints)
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE products (price NUMERIC, recommended_retail_price NUMERIC, shop_price NUMERIC CHECK (shop_price >= 0))")
        database.executemany("INSERT INTO products VALUES (?, ?, NULL)", [(100, 200), (0, 200), (75, None), (0, None), (100, 0)])
        database.execute(operations.execute.call_args.args[0])
        assert database.execute("SELECT price, recommended_retail_price, shop_price FROM products").fetchall() == [
            (100, 200, 200), (0, 200, 200), (75, None, 75), (0, None, 0), (100, 0, 0),
        ]
    migration.downgrade()
    operations.drop_column.assert_called_once_with("products", "shop_price")
