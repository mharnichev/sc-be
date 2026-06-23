from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import BarberService, Booking, BookingStatus
from app.models.customer import Customer
from app.models.promotion import Promotion, PromotionDiscountType, PromotionEligibilityType
from app.schemas.promotion import normalize_promotion_code

KYIV_TZ = ZoneInfo("Europe/Kyiv")


class PromotionService:
    def normalize_code(self, code: str) -> str:
        return normalize_promotion_code(code)

    def subtotal_amount(self, services: Sequence[BarberService]) -> int:
        return sum(int(getattr(item, "price", 0) or 0) for item in services)

    def discount_amount(self, subtotal_amount: int, promotion: Promotion) -> int:
        if subtotal_amount <= 0:
            return 0
        if promotion.discount_type != PromotionDiscountType.percent:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported promotion discount type")
        raw_discount = Decimal(subtotal_amount) * Decimal(promotion.discount_percent) / Decimal(100)
        return int(raw_discount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def total_amount(self, subtotal_amount: int, discount_amount: int) -> int:
        return max(subtotal_amount - discount_amount, 0)

    async def ensure_unique_code(
        self,
        session: AsyncSession,
        code: str,
        *,
        exclude_promotion_id: int | None = None,
    ) -> None:
        stmt = select(Promotion.id).where(Promotion.code == self.normalize_code(code))
        if exclude_promotion_id is not None:
            stmt = stmt.where(Promotion.id != exclude_promotion_id)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Promotion code already exists")

    async def get_active_by_code(
        self,
        session: AsyncSession,
        code: str,
        *,
        at: datetime,
    ) -> Promotion:
        normalized_code = self.normalize_code(code)
        promotion = (
            await session.execute(select(Promotion).where(Promotion.code == normalized_code))
        ).scalar_one_or_none()
        if promotion is None or not promotion.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion is not active")
        at = self._normalize_datetime(at)
        if promotion.starts_at is not None and self._normalize_datetime(promotion.starts_at) > at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion is not active yet")
        if promotion.ends_at is not None and self._normalize_datetime(promotion.ends_at) < at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion has expired")
        return promotion

    async def apply_to_booking(
        self,
        session: AsyncSession,
        *,
        booking: Booking,
        promotion_code: str | None,
        customer: Customer,
        services: Sequence[BarberService],
        at: datetime,
    ) -> None:
        subtotal_amount = self.subtotal_amount(services)
        booking.subtotal_amount = subtotal_amount

        if not promotion_code:
            booking.promotion_id = None
            booking.promotion_code_snapshot = None
            booking.promotion_name_uk_snapshot = None
            booking.promotion_name_en_snapshot = None
            booking.promotion_discount_percent_snapshot = None
            booking.promotion_discount_amount = 0
            booking.total_amount = subtotal_amount
            return

        promotion = await self.get_active_by_code(session, promotion_code, at=at)
        await self.ensure_customer_eligible(session, promotion=promotion, customer=customer, at=at)
        discount_amount = self.discount_amount(subtotal_amount, promotion)

        booking.promotion_id = promotion.id
        booking.promotion_code_snapshot = promotion.code
        booking.promotion_name_uk_snapshot = promotion.name_uk
        booking.promotion_name_en_snapshot = promotion.name_en
        booking.promotion_discount_percent_snapshot = promotion.discount_percent
        booking.promotion_discount_amount = discount_amount
        booking.total_amount = self.total_amount(subtotal_amount, discount_amount)

    async def ensure_customer_eligible(
        self,
        session: AsyncSession,
        *,
        promotion: Promotion,
        customer: Customer,
        at: datetime,
    ) -> None:
        if promotion.eligibility_type == PromotionEligibilityType.all_customers:
            return
        if promotion.eligibility_type != PromotionEligibilityType.inactive_customers:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported promotion eligibility type")

        inactive_days = promotion.inactive_days or 90
        cutoff = self._normalize_datetime(at) - timedelta(days=inactive_days)
        last_visit_at = await self.customer_last_visit_at(session, customer)
        if last_visit_at is not None and self._normalize_datetime(last_visit_at) > cutoff:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Promotion is only available to customers inactive for {inactive_days} days",
            )

    async def customer_last_visit_at(self, session: AsyncSession, customer: Customer) -> datetime | None:
        booking_last_visit_at = (
            await session.execute(
                select(func.max(Booking.end_at)).where(
                    Booking.customer_id == customer.id,
                    Booking.status == BookingStatus.completed,
                )
            )
        ).scalar_one_or_none()
        imported_last_visit_at = getattr(customer, "imported_last_visit_at", None)
        candidates = [item for item in (booking_last_visit_at, imported_last_visit_at) if item is not None]
        if not candidates:
            return None
        return max(self._normalize_datetime(item) for item in candidates)

    def complete_update_data(self, promotion: Promotion, data: dict) -> dict:
        eligibility_type = data.get("eligibility_type", promotion.eligibility_type)
        if eligibility_type == PromotionEligibilityType.inactive_customers and "inactive_days" not in data:
            data["inactive_days"] = promotion.inactive_days or 90
        if eligibility_type != PromotionEligibilityType.inactive_customers:
            data["inactive_days"] = None

        starts_at = data.get("starts_at", promotion.starts_at)
        ends_at = data.get("ends_at", promotion.ends_at)
        if starts_at is not None and ends_at is not None and self._normalize_datetime(ends_at) <= self._normalize_datetime(starts_at):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ends_at must be after starts_at")
        return data

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=KYIV_TZ)
        return value.astimezone(KYIV_TZ)
