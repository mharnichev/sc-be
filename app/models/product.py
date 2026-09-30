from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("volume_ml IS NULL OR volume_ml > 0", name="products_volume_ml_positive"),
        CheckConstraint(
            "variant_group_key IS NULL OR volume_ml IS NOT NULL",
            name="products_variant_group_requires_volume",
        ),
        Index("ix_products_top_sort", "is_top", "top_score"),
        Index("ix_products_variant_group_volume", "variant_group_key", "volume_ml"),
        CheckConstraint("stock_quantity >= 0", name="products_stock_quantity_nonnegative"),
        CheckConstraint("reserved_quantity >= 0", name="products_reserved_quantity_nonnegative"),
        CheckConstraint("stock_quantity - reserved_quantity >= 0", name="products_available_quantity_nonnegative"),
        UniqueConstraint("barcode", name="uq_products_barcode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    old_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    package_size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingredients: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    recommended_retail_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allow_backorder: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    availability_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attributes_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    variant_group_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    volume_ml: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_top: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    top_score: Mapped[Decimal] = mapped_column(Numeric(8, 6), default=0, nullable=False)
    top_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top_unique_views_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top_paid_orders_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top_purchased_units_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id", ondelete="SET NULL"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    brand = relationship("Brand", back_populates="products")
    category = relationship("Category", back_populates="products")
    order_items = relationship("OrderItem", back_populates="product")
    inventory_movements = relationship("InventoryMovement", back_populates="product")
    inventory_receipt_items = relationship("InventoryReceiptItem", back_populates="product")
    inventory_count_items = relationship("InventoryCountItem", back_populates="product")
    images = relationship(
        "ProductImage",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.sort_order",
    )
    image_variants = relationship("ProductImageVariant", back_populates="product", cascade="all, delete-orphan")
    cart_items = relationship("CustomerCartItem", back_populates="product", cascade="all, delete-orphan")
    wishlist_items = relationship("CustomerWishlistItem", back_populates="product", cascade="all, delete-orphan")
    reviews = relationship("ProductReview", back_populates="product", cascade="all, delete-orphan")
    views = relationship("ProductView", back_populates="product", cascade="all, delete-orphan")

    @property
    def available_quantity(self) -> int:
        return (self.stock_quantity or 0) - (self.reserved_quantity or 0)
