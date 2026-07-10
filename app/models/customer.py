from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    surname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_total_spent: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    imported_last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_is_new_client: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    orders = relationship("Order", back_populates="customer")
    bookings = relationship("Booking", back_populates="customer")
    cart_items = relationship("CustomerCartItem", back_populates="customer", cascade="all, delete-orphan")
    wishlist_items = relationship("CustomerWishlistItem", back_populates="customer", cascade="all, delete-orphan")
    product_reviews = relationship("ProductReview", back_populates="customer", cascade="all, delete-orphan")
    product_review_comments = relationship(
        "ProductReviewComment",
        back_populates="customer",
        cascade="all, delete-orphan",
    )

    @property
    def is_verified(self) -> bool:
        return self.phone_verified_at is not None
