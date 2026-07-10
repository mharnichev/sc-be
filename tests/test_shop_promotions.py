from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.category import Category
from app.models.product import Product
from app.models.shop_promotion import (
    ShopPromotion,
    ShopPromotionDiscountType,
    ShopPromotionTrigger,
)
from app.schemas.shop_promotion import ShopPromotionCreate
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
        include_subcategories=True,
        is_active=True,
        products=products or [],
        categories=categories or [],
        brands=[],
    )


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
