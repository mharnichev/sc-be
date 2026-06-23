from __future__ import annotations

import enum

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class PromotionDiscountType(str, enum.Enum):
    percent = "percent"


class PromotionEligibilityType(str, enum.Enum):
    all_customers = "all_customers"
    inactive_customers = "inactive_customers"


class Promotion(TimestampMixin, Base):
    __tablename__ = "promotions"
    __table_args__ = (
        Index("ix_promotions_code", "code", unique=True),
        Index("ix_promotions_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name_uk: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str] = mapped_column(String(255), nullable=False)
    description_uk: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_type: Mapped[PromotionDiscountType] = mapped_column(
        Enum(PromotionDiscountType),
        default=PromotionDiscountType.percent,
        nullable=False,
    )
    discount_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    eligibility_type: Mapped[PromotionEligibilityType] = mapped_column(
        Enum(PromotionEligibilityType),
        default=PromotionEligibilityType.all_customers,
        nullable=False,
    )
    inactive_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    bookings = relationship("Booking", back_populates="promotion")
