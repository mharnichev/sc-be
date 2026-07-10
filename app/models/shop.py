from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class ProductImage(TimestampMixin, Base):
    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id", ondelete="SET NULL"), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    alt: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    product = relationship("Product", back_populates="images")
    upload = relationship("Upload")


class CustomerCartItem(TimestampMixin, Base):
    __tablename__ = "customer_cart_items"
    __table_args__ = (
        UniqueConstraint("customer_id", "product_id", name="uq_customer_cart_items_customer_product"),
        CheckConstraint("quantity > 0", name="customer_cart_items_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    customer = relationship("Customer", back_populates="cart_items")
    product = relationship("Product", back_populates="cart_items")


class CustomerWishlistItem(TimestampMixin, Base):
    __tablename__ = "customer_wishlist_items"
    __table_args__ = (UniqueConstraint("customer_id", "product_id", name="uq_customer_wishlist_items_customer_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)

    customer = relationship("Customer", back_populates="wishlist_items")
    product = relationship("Product", back_populates="wishlist_items")


class ProductReview(TimestampMixin, Base):
    __tablename__ = "product_reviews"
    __table_args__ = (
        UniqueConstraint("product_id", "customer_id", name="uq_product_reviews_product_customer"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="product_reviews_rating_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    product = relationship("Product", back_populates="reviews")
    customer = relationship("Customer", back_populates="product_reviews")
    comments = relationship("ProductReviewComment", back_populates="review", cascade="all, delete-orphan")


class ProductReviewComment(TimestampMixin, Base):
    __tablename__ = "product_review_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("product_reviews.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    comment: Mapped[str] = mapped_column(Text, nullable=False)

    review = relationship("ProductReview", back_populates="comments")
    customer = relationship("Customer", back_populates="product_review_comments")


class ProductView(TimestampMixin, Base):
    __tablename__ = "product_views"
    __table_args__ = (
        UniqueConstraint("product_id", "visitor_hash", "viewed_on", name="uq_product_views_product_visitor_day"),
        Index("ix_product_views_product_viewed_on", "product_id", "viewed_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    visitor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    viewed_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    product = relationship("Product", back_populates="views")


class DeliveryCache(TimestampMixin, Base):
    __tablename__ = "delivery_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
