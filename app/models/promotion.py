from __future__ import annotations

import enum

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class PromotionDiscountType(str, enum.Enum):
    percent = "percent"


class PromotionEligibilityType(str, enum.Enum):
    all_customers = "all_customers"
    inactive_customers = "inactive_customers"
    military_customers = "military_customers"


promotion_masters = Table(
    "promotion_masters",
    Base.metadata,
    Column("promotion_id", ForeignKey("promotions.id", ondelete="CASCADE"), primary_key=True),
    Column("master_id", ForeignKey("masters.id", ondelete="CASCADE"), primary_key=True),
)

promotion_base_services = Table(
    "promotion_base_services",
    Base.metadata,
    Column("promotion_id", ForeignKey("promotions.id", ondelete="CASCADE"), primary_key=True),
    Column("base_service_id", ForeignKey("base_services.id", ondelete="CASCADE"), primary_key=True),
)


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
    applies_to_all_masters: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    applies_to_all_services: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    bookings = relationship("Booking", back_populates="promotion")
    masters = relationship("Master", secondary=promotion_masters)
    base_services = relationship("BaseService", secondary=promotion_base_services)

    @property
    def master_ids(self) -> list[int]:
        return [item.id for item in self.masters]

    @property
    def base_service_ids(self) -> list[int]:
        return [item.id for item in self.base_services]
