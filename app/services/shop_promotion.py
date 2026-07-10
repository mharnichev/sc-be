from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.shop_promotion import (
    ShopPromotion,
    ShopPromotionDiscountType,
    ShopPromotionTrigger,
)
from app.schemas.shop_promotion import normalize_shop_promotion_code

MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class ShopPriceResult:
    base_price: Decimal
    price: Decimal
    discount_amount: Decimal
    discount_percent: Decimal | None
    promotion_id: int | None = None
    promotion_name: str | None = None
    promotion_code: str | None = None
    promotion_trigger: ShopPromotionTrigger | None = None


class ShopPromotionService:
    promotion_options = (
        selectinload(ShopPromotion.products),
        selectinload(ShopPromotion.categories),
        selectinload(ShopPromotion.brands),
    )

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    @staticmethod
    def is_active_at(promotion: ShopPromotion, at: datetime) -> bool:
        if not promotion.is_active:
            return False
        comparable_at = at
        if promotion.starts_at is not None:
            if promotion.starts_at.tzinfo is None and comparable_at.tzinfo is not None:
                comparable_at = comparable_at.replace(tzinfo=None)
            elif promotion.starts_at.tzinfo is not None and comparable_at.tzinfo is None:
                comparable_at = comparable_at.replace(tzinfo=promotion.starts_at.tzinfo)
            if comparable_at < promotion.starts_at:
                return False
        if promotion.ends_at is not None:
            comparable_at = at
            if promotion.ends_at.tzinfo is None and comparable_at.tzinfo is not None:
                comparable_at = comparable_at.replace(tzinfo=None)
            elif promotion.ends_at.tzinfo is not None and comparable_at.tzinfo is None:
                comparable_at = comparable_at.replace(tzinfo=promotion.ends_at.tzinfo)
            if comparable_at >= promotion.ends_at:
                return False
        return True

    @staticmethod
    def category_ancestor_ids(category_id: int | None, parents: dict[int, int | None]) -> set[int]:
        ancestors: set[int] = set()
        current = category_id
        while current is not None and current not in ancestors:
            ancestors.add(current)
            current = parents.get(current)
        return ancestors

    @classmethod
    def matches_product(
        cls,
        promotion: ShopPromotion,
        product: Product,
        *,
        category_parents: dict[int, int | None],
    ) -> bool:
        if promotion.applies_to_all_products:
            return True
        if product.id in promotion.product_ids:
            return True
        if product.brand_id is not None and product.brand_id in promotion.brand_ids:
            return True
        if product.category_id is None:
            return False
        category_ids = (
            cls.category_ancestor_ids(product.category_id, category_parents)
            if promotion.include_subcategories
            else {product.category_id}
        )
        return bool(category_ids.intersection(promotion.category_ids))

    @classmethod
    def apply_promotion(cls, base_price: Decimal, promotion: ShopPromotion) -> Decimal:
        value = Decimal(promotion.discount_value)
        if promotion.discount_type == ShopPromotionDiscountType.percent:
            price = base_price * (Decimal("1") - value / Decimal("100"))
        elif promotion.discount_type == ShopPromotionDiscountType.fixed_amount:
            price = base_price - value
        else:
            price = value
        return cls._money(max(Decimal("0.00"), price))

    @classmethod
    def calculate_product_price(
        cls,
        product: Product,
        promotions: list[ShopPromotion],
        *,
        category_parents: dict[int, int | None],
        at: datetime | None = None,
    ) -> ShopPriceResult:
        at = at or datetime.now(UTC)
        base_price = cls._money(Decimal(product.price))
        candidates: list[tuple[Decimal, int, int, ShopPromotion]] = []
        for promotion in promotions:
            if not cls.is_active_at(promotion, at):
                continue
            if not cls.matches_product(promotion, product, category_parents=category_parents):
                continue
            price = cls.apply_promotion(base_price, promotion)
            if price >= base_price:
                continue
            candidates.append((price, promotion.priority, promotion.id, promotion))

        if not candidates:
            return ShopPriceResult(
                base_price=base_price,
                price=base_price,
                discount_amount=Decimal("0.00"),
                discount_percent=None,
            )

        price, _priority, _promotion_id, promotion = min(candidates, key=lambda item: item[:3])
        discount_amount = cls._money(base_price - price)
        discount_percent = cls._money(discount_amount / base_price * Decimal("100")) if base_price else None
        return ShopPriceResult(
            base_price=base_price,
            price=price,
            discount_amount=discount_amount,
            discount_percent=discount_percent,
            promotion_id=promotion.id,
            promotion_name=promotion.name,
            promotion_code=promotion.code,
            promotion_trigger=promotion.trigger,
        )

    async def _category_parents(self, session: AsyncSession) -> dict[int, int | None]:
        rows = (await session.execute(select(Category.id, Category.parent_id))).all()
        return {row.id: row.parent_id for row in rows}

    async def _automatic_promotions(self, session: AsyncSession, *, at: datetime) -> list[ShopPromotion]:
        stmt = (
            select(ShopPromotion)
            .options(*self.promotion_options)
            .where(
                ShopPromotion.trigger == ShopPromotionTrigger.automatic,
                ShopPromotion.is_active.is_(True),
                or_(ShopPromotion.starts_at.is_(None), ShopPromotion.starts_at <= at),
                or_(ShopPromotion.ends_at.is_(None), ShopPromotion.ends_at > at),
            )
            .order_by(ShopPromotion.priority.asc(), ShopPromotion.id.asc())
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _promotion_by_code(
        self,
        session: AsyncSession,
        code: str,
        *,
        at: datetime,
        for_update: bool = False,
    ) -> ShopPromotion:
        normalized = normalize_shop_promotion_code(code)
        stmt = (
            select(ShopPromotion)
            .options(*self.promotion_options)
            .where(
                ShopPromotion.trigger == ShopPromotionTrigger.promocode,
                ShopPromotion.code == normalized,
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        promotion = (
            await session.execute(stmt)
        ).scalar_one_or_none()
        if promotion is None or not self.is_active_at(promotion, at):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promo code is invalid or inactive")
        return promotion

    async def _validate_usage_limits(
        self,
        session: AsyncSession,
        promotion: ShopPromotion,
        *,
        customer_phone: str | None,
    ) -> None:
        base_conditions = (
            Order.promo_code == promotion.code,
            Order.status != OrderStatus.cancelled,
        )
        if promotion.usage_limit is not None:
            total_uses = (
                await session.execute(select(func.count(Order.id)).where(*base_conditions))
            ).scalar_one()
            if total_uses >= promotion.usage_limit:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promo code usage limit reached")
        if promotion.usage_limit_per_customer is not None:
            if not customer_phone:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Customer phone is required for this promo code",
                )
            customer_uses = (
                await session.execute(
                    select(func.count(Order.id)).where(*base_conditions, Order.customer_phone == customer_phone)
                )
            ).scalar_one()
            if customer_uses >= promotion.usage_limit_per_customer:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer promo code limit reached")

    async def price_products(
        self,
        session: AsyncSession,
        products: list[Product],
        *,
        promo_code: str | None = None,
        customer_phone: str | None = None,
        validate_code_usage: bool = False,
        lock_code: bool = False,
        at: datetime | None = None,
    ) -> dict[int, ShopPriceResult]:
        at = at or datetime.now(UTC)
        automatic = await self._automatic_promotions(session, at=at)
        promotions = list(automatic)
        code_promotion: ShopPromotion | None = None
        if promo_code:
            code_promotion = await self._promotion_by_code(session, promo_code, at=at, for_update=lock_code)
            if validate_code_usage:
                await self._validate_usage_limits(session, code_promotion, customer_phone=customer_phone)
            promotions.append(code_promotion)

        category_parents = await self._category_parents(session)
        prices = {
            product.id: self.calculate_product_price(
                product,
                promotions,
                category_parents=category_parents,
                at=at,
            )
            for product in products
        }
        if code_promotion is not None:
            code_has_discount = any(
                self.matches_product(code_promotion, product, category_parents=category_parents)
                and self.apply_promotion(Decimal(product.price), code_promotion) < Decimal(product.price)
                for product in products
            )
            if not code_has_discount:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Promo code does not apply to these products",
                )
        return prices


shop_promotion_service = ShopPromotionService()
