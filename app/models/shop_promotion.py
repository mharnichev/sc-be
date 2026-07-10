from __future__ import annotations

import enum

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class ShopPromotionTrigger(str, enum.Enum):
    automatic = "automatic"
    promocode = "promocode"


class ShopPromotionDiscountType(str, enum.Enum):
    percent = "percent"
    fixed_amount = "fixed_amount"
    fixed_price = "fixed_price"


shop_promotion_products = Table(
    "shop_promotion_products",
    Base.metadata,
    Column("promotion_id", ForeignKey("shop_promotions.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
)

shop_promotion_categories = Table(
    "shop_promotion_categories",
    Base.metadata,
    Column("promotion_id", ForeignKey("shop_promotions.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
)

shop_promotion_brands = Table(
    "shop_promotion_brands",
    Base.metadata,
    Column("promotion_id", ForeignKey("shop_promotions.id", ondelete="CASCADE"), primary_key=True),
    Column("brand_id", ForeignKey("brands.id", ondelete="CASCADE"), primary_key=True),
)


class ShopPromotion(TimestampMixin, Base):
    __tablename__ = "shop_promotions"
    __table_args__ = (
        Index("ix_shop_promotions_code", "code", unique=True),
        Index("ix_shop_promotions_active_period", "is_active", "starts_at", "ends_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[ShopPromotionTrigger] = mapped_column(Enum(ShopPromotionTrigger), nullable=False)
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    discount_type: Mapped[ShopPromotionDiscountType] = mapped_column(Enum(ShopPromotionDiscountType), nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_limit_per_customer: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applies_to_all_products: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    include_subcategories: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    products = relationship("Product", secondary=shop_promotion_products, lazy="selectin")
    categories = relationship("Category", secondary=shop_promotion_categories, lazy="selectin")
    brands = relationship("Brand", secondary=shop_promotion_brands, lazy="selectin")

    @property
    def product_ids(self) -> list[int]:
        return [item.id for item in self.products]

    @property
    def category_ids(self) -> list[int]:
        return [item.id for item in self.categories]

    @property
    def brand_ids(self) -> list[int]:
        return [item.id for item in self.brands]
