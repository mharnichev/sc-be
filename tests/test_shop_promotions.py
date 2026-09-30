from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.category import Category
from app.models.brand import Brand
from app.models.product import Product
from app.models.shop_promotion import (
    ShopPromotion,
    ShopPromotionDiscountType,
    ShopPromotionStatus,
    ShopPromotionTrigger,
)
from app.schemas.shop_promotion import ShopPromotionCreate, ShopPromotionResponse
from app.services.shop_promotion import ShopPromotionService


def promotion(
    *,
    promotion_id: int,
    trigger: ShopPromotionTrigger = ShopPromotionTrigger.automatic,
    code: str | None = None,
    discount_type: ShopPromotionDiscountType = ShopPromotionDiscountType.percent,
    discount_value: Decimal = Decimal("10.00"),
    priority: int = 100,
    applies_to_all_products: bool = False,
    products: list[Product] | None = None,
    categories: list[Category] | None = None,
    brands: list[Brand] | None = None,
    include_subcategories: bool = True,
    is_active: bool = True,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> ShopPromotion:
    return ShopPromotion(
        id=promotion_id,
        name=f"Promotion {promotion_id}",
        trigger=trigger,
        code=code,
        discount_type=discount_type,
        discount_value=discount_value,
        priority=priority,
        applies_to_all_products=applies_to_all_products,
        include_subcategories=include_subcategories,
        is_active=is_active,
        starts_at=starts_at,
        ends_at=ends_at,
        products=products or [],
        categories=categories or [],
        brands=brands or [],
    )


@pytest.mark.anyio
async def test_create_shop_promotion_assigns_scope_before_session_add(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relationship assignment must happen while a new promotion is transient."""
    from app.api.v1.routes import shop_promotions as routes

    payload = ShopPromotionCreate(
        name="Summer sale",
        trigger=ShopPromotionTrigger.automatic,
        discount_type=ShopPromotionDiscountType.percent,
        discount_value=Decimal("15"),
        product_ids=[64],
    )
    added_scope: dict[str, list[int]] = {}

    class ScalarResult:
        def scalar_one_or_none(self):
            return None

    class FakeSession:
        async def execute(self, _statement):
            return ScalarResult()

        def add(self, promotion):
            added_scope.update({
                "product_ids": promotion.product_ids,
                "category_ids": promotion.category_ids,
                "brand_ids": promotion.brand_ids,
            })

        async def commit(self):
            return None

    async def fake_load_entities(_session, model, ids, _label):
        if model is Product:
            return [Product(id=64, name="Product", slug="product", price=Decimal("100"))]
        return []

    async def fake_get_for_response(_session, _promotion_id):
        now = datetime.now(UTC)
        return {
            "id": 1,
            "created_at": now,
            "updated_at": now,
            **payload.model_dump(),
        }

    monkeypatch.setattr(routes, "load_entities", fake_load_entities)
    monkeypatch.setattr(routes, "get_for_response", fake_get_for_response)

    result = await routes.create_shop_promotion(payload, SimpleNamespace(is_superuser=True), FakeSession())

    assert result.id == 1
    assert added_scope == {"product_ids": [64], "category_ids": [], "brand_ids": []}


@pytest.mark.anyio
async def test_list_shop_promotion_products_returns_scope_matches_and_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1.routes import shop_promotions as routes

    matching = Product(id=64, name="Visible product", slug="visible-product", price=Decimal("100"), is_active=True)
    hidden = Product(id=65, name="Hidden product", slug="hidden-product", price=Decimal("80"), is_active=False)
    unrelated = Product(id=66, name="Other product", slug="other-product", price=Decimal("60"), is_active=True)
    saved = promotion(promotion_id=9, products=[matching, hidden])

    class ScalarResult:
        def scalars(self):
            return self

        def all(self):
            return [matching, hidden, unrelated]

    class FakeVisibility:
        def category_parents(self):
            return {}

        def product_states(self, products):
            return {
                item.id: SimpleNamespace(
                    is_effectively_visible=item.is_active,
                    hidden_reason=None if item.is_active else "product",
                )
                for item in products
            }

    class FakeSession:
        async def execute(self, _statement):
            return ScalarResult()

    async def fake_get_for_response(_session, _promotion_id):
        return saved

    async def fake_visibility_load(_session):
        return FakeVisibility()

    monkeypatch.setattr(routes, "get_for_response", fake_get_for_response)
    monkeypatch.setattr(routes.CatalogVisibility, "load", fake_visibility_load)

    result = await routes.list_shop_promotion_products(9, SimpleNamespace(is_superuser=True), FakeSession())

    assert result.affected_products_count == 2
    assert [item.product_id for item in result.products] == [64, 65]
    assert result.products[1].is_effectively_visible is False
    assert result.products[1].hidden_reason == "product"


def test_promocode_schema_normalizes_code_and_requires_scope() -> None:
    payload = ShopPromotionCreate(
        name="Welcome",
        trigger=ShopPromotionTrigger.promocode,
        code=" welcome10 ",
        discount_type=ShopPromotionDiscountType.percent,
        discount_value=Decimal("10"),
        product_ids=[1, 2],
    )

    assert payload.code == "WELCOME10"
    assert payload.product_ids == [1, 2]

    with pytest.raises(ValidationError):
        ShopPromotionCreate(
            name="Broken",
            trigger=ShopPromotionTrigger.automatic,
            discount_type=ShopPromotionDiscountType.percent,
            discount_value=Decimal("15"),
        )


def test_automatic_promotion_applies_to_category_descendants() -> None:
    root = Category(id=1, name="Tools", slug="tools", is_active=True)
    product = Product(id=5, name="Clipper", slug="clipper", price=Decimal("100.00"), category_id=3)
    rule = promotion(promotion_id=1, categories=[root], discount_value=Decimal("15"))

    result = ShopPromotionService.calculate_product_price(
        product,
        [rule],
        category_parents={1: None, 2: 1, 3: 2},
        at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert result.base_price == Decimal("100.00")
    assert result.price == Decimal("85.00")
    assert result.discount_amount == Decimal("15.00")
    assert result.promotion_id == rule.id


def test_best_deal_wins_between_automatic_and_promocode() -> None:
    product = Product(id=5, name="Clipper", slug="clipper", price=Decimal("100.00"))
    automatic = promotion(
        promotion_id=1,
        applies_to_all_products=True,
        discount_type=ShopPromotionDiscountType.percent,
        discount_value=Decimal("10"),
    )
    promocode = promotion(
        promotion_id=2,
        trigger=ShopPromotionTrigger.promocode,
        code="SALE15",
        applies_to_all_products=True,
        discount_type=ShopPromotionDiscountType.fixed_price,
        discount_value=Decimal("85"),
    )

    result = ShopPromotionService.calculate_product_price(
        product,
        [automatic, promocode],
        category_parents={},
        at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert result.price == Decimal("85.00")
    assert result.promotion_code == "SALE15"
    assert result.discount_percent == Decimal("15.00")


def test_fixed_amount_never_produces_negative_price() -> None:
    product = Product(id=5, name="Clipper", slug="clipper", price=Decimal("40.00"))
    rule = promotion(
        promotion_id=1,
        applies_to_all_products=True,
        discount_type=ShopPromotionDiscountType.fixed_amount,
        discount_value=Decimal("100"),
    )

    result = ShopPromotionService.calculate_product_price(
        product,
        [rule],
        category_parents={},
        at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert result.price == Decimal("0.00")
    assert result.discount_amount == Decimal("40.00")


def test_product_scope_can_target_one_or_many_products() -> None:
    products = [
        Product(id=1, name="One", slug="one", price=Decimal("100.00")),
        Product(id=2, name="Two", slug="two", price=Decimal("200.00")),
        Product(id=3, name="Three", slug="three", price=Decimal("300.00")),
    ]
    rule = promotion(promotion_id=1, products=products[:2], discount_value=Decimal("10"))

    prices = [
        ShopPromotionService.calculate_product_price(product, [rule], category_parents={}, at=datetime(2026, 7, 10, tzinfo=UTC))
        for product in products
    ]

    assert [price.price for price in prices] == [Decimal("90.00"), Decimal("180.00"), Decimal("300.00")]


def test_brand_scope_applies_only_to_matching_brand() -> None:
    brand = Brand(id=7, name="Brand", slug="brand")
    matching = Product(id=1, name="Matching", slug="matching", price=Decimal("100.00"), brand_id=7)
    other = Product(id=2, name="Other", slug="other", price=Decimal("100.00"), brand_id=8)
    rule = promotion(promotion_id=1, brands=[brand], discount_value=Decimal("20"))

    matching_price = ShopPromotionService.calculate_product_price(matching, [rule], category_parents={}, at=datetime(2026, 7, 10, tzinfo=UTC))
    other_price = ShopPromotionService.calculate_product_price(other, [rule], category_parents={}, at=datetime(2026, 7, 10, tzinfo=UTC))

    assert matching_price.price == Decimal("80.00")
    assert other_price.price == Decimal("100.00")


def test_include_subcategories_can_be_disabled() -> None:
    root = Category(id=1, name="Tools", slug="tools")
    product = Product(id=5, name="Clipper", slug="clipper", price=Decimal("100.00"), category_id=3)
    rule = promotion(promotion_id=1, categories=[root], include_subcategories=False, discount_value=Decimal("15"))

    result = ShopPromotionService.calculate_product_price(
        product,
        [rule],
        category_parents={1: None, 3: 1},
        at=datetime(2026, 7, 10, tzinfo=UTC),
    )

    assert result.price == Decimal("100.00")


@pytest.mark.parametrize(
    ("discount_type", "discount_value", "expected"),
    [
        (ShopPromotionDiscountType.fixed_amount, Decimal("15"), Decimal("85.00")),
        (ShopPromotionDiscountType.fixed_price, Decimal("70"), Decimal("70.00")),
    ],
)
def test_fixed_discount_types(discount_type: ShopPromotionDiscountType, discount_value: Decimal, expected: Decimal) -> None:
    product = Product(id=5, name="Clipper", slug="clipper", price=Decimal("100.00"))
    rule = promotion(
        promotion_id=1,
        applies_to_all_products=True,
        discount_type=discount_type,
        discount_value=discount_value,
    )

    result = ShopPromotionService.calculate_product_price(product, [rule], category_parents={}, at=datetime(2026, 7, 10, tzinfo=UTC))

    assert result.price == expected


def test_period_boundaries_and_states_are_kyiv_aware() -> None:
    start = datetime(2026, 7, 10, 12, tzinfo=UTC)
    end = datetime(2026, 7, 10, 13, tzinfo=UTC)
    product = Product(id=5, name="Clipper", slug="clipper", price=Decimal("100.00"))
    rule = promotion(promotion_id=1, applies_to_all_products=True, starts_at=start, ends_at=end)

    at_start = ShopPromotionService.calculate_product_price(product, [rule], category_parents={}, at=start)
    at_end = ShopPromotionService.calculate_product_price(product, [rule], category_parents={}, at=end)
    status_now = datetime.now(UTC)
    scheduled = promotion(promotion_id=2, applies_to_all_products=True, starts_at=status_now + timedelta(days=1))
    expired = promotion(promotion_id=3, applies_to_all_products=True, ends_at=status_now - timedelta(days=1))
    disabled = promotion(promotion_id=4, applies_to_all_products=True, is_active=False)

    assert at_start.price == Decimal("90.00")
    assert at_end.price == Decimal("100.00")
    assert ShopPromotionResponse.model_validate(
        scheduled.__dict__ | {"created_at": start, "updated_at": start}
    ).status == ShopPromotionStatus.scheduled
    assert ShopPromotionResponse.model_validate(
        expired.__dict__ | {"created_at": start, "updated_at": start}
    ).status == ShopPromotionStatus.expired
    assert ShopPromotionResponse.model_validate(
        disabled.__dict__ | {"created_at": start, "updated_at": start}
    ).status == ShopPromotionStatus.disabled


def test_automatic_usage_limits_are_rejected_instead_of_ignored() -> None:
    with pytest.raises(ValidationError, match="usage limits"):
        ShopPromotionCreate(
            name="Automatic",
            trigger=ShopPromotionTrigger.automatic,
            discount_type=ShopPromotionDiscountType.percent,
            discount_value=Decimal("10"),
            usage_limit=1,
            applies_to_all_products=True,
        )


def test_same_final_price_uses_priority_as_tie_breaker() -> None:
    product = Product(id=5, name="Clipper", slug="clipper", price=Decimal("100.00"))
    winner = promotion(promotion_id=2, applies_to_all_products=True, priority=10, discount_value=Decimal("20"))
    loser = promotion(promotion_id=1, applies_to_all_products=True, priority=20, discount_value=Decimal("20"))

    result = ShopPromotionService.calculate_product_price(product, [loser, winner], category_parents={}, at=datetime(2026, 7, 10, tzinfo=UTC))

    assert result.promotion_id == winner.id
