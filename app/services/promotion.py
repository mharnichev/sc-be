from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.booking import BarberService, Booking, BookingStatus
from app.models.customer import Customer
from app.models.promotion import Promotion, PromotionApplicationMode, PromotionDiscountType, PromotionEligibilityType
from app.schemas.promotion import normalize_promotion_code
from app.utils.booking_identity import phone_aliases

KYIV_TZ = ZoneInfo("Europe/Kyiv")


class PromotionService:
    def normalize_code(self, code: str) -> str:
        return normalize_promotion_code(code)

    def service_price(self, service: BarberService, service_prices: Mapping[int, int] | None = None) -> int:
        service_id = getattr(service, "id", None)
        if service_prices is not None and service_id in service_prices:
            return int(service_prices[service_id])
        return int(getattr(service, "price", 0) or 0)

    def subtotal_amount(
        self,
        services: Sequence[BarberService],
        service_prices: Mapping[int, int] | None = None,
    ) -> int:
        return sum(self.service_price(item, service_prices) for item in services)

    def discount_amount(self, subtotal_amount: int, promotion: Promotion) -> int:
        if subtotal_amount <= 0:
            return 0
        if promotion.discount_type != PromotionDiscountType.percent:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported promotion discount type")
        raw_discount = Decimal(subtotal_amount) * Decimal(promotion.discount_percent) / Decimal(100)
        return int(raw_discount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def total_amount(self, subtotal_amount: int, discount_amount: int) -> int:
        return max(subtotal_amount - discount_amount, 0)

    def promotion_master_ids(self, promotion: Promotion) -> set[int]:
        return {int(item) for item in (getattr(promotion, "master_ids", None) or [])}

    def promotion_base_service_ids(self, promotion: Promotion) -> set[int]:
        return {int(item) for item in (getattr(promotion, "base_service_ids", None) or [])}

    def applies_to_service(self, promotion: Promotion, service: BarberService) -> bool:
        if getattr(promotion, "applies_to_all_masters", True) is False:
            master_id = getattr(service, "master_id", None)
            if master_id is None or int(master_id) not in self.promotion_master_ids(promotion):
                return False

        if getattr(promotion, "applies_to_all_services", True) is False:
            base_service_id = getattr(service, "base_service_id", None)
            if base_service_id is None or int(base_service_id) not in self.promotion_base_service_ids(promotion):
                return False

        return True

    def eligible_services(self, services: Sequence[BarberService], promotion: Promotion) -> list[BarberService]:
        return [item for item in services if self.applies_to_service(promotion, item)]

    def public_promotion_payload(self, service: BarberService, promotion: Promotion) -> dict:
        price = int(getattr(service, "price", 0) or 0)
        discount_amount = self.discount_amount(price, promotion)
        return {
            **self.public_offer_payload(promotion),
            "discount_amount": discount_amount,
            "promotional_price": self.total_amount(price, discount_amount),
        }

    def public_offer_payload(self, promotion: Promotion) -> dict:
        return {
            "id": promotion.id,
            "code": None if self.is_automatic(promotion) else promotion.code,
            "application_mode": getattr(promotion, "application_mode", None) or "code",
            "requires_code": not self.is_automatic(promotion),
            "eligibility_type": promotion.eligibility_type,
            "conditional": promotion.eligibility_type != PromotionEligibilityType.all_customers,
            "eligibility_scope": "barbershop" if promotion.eligibility_type == PromotionEligibilityType.first_visit else None,
            "applies_to_all_masters": promotion.applies_to_all_masters,
            "applies_to_all_services": promotion.applies_to_all_services,
            "master_ids": sorted(self.promotion_master_ids(promotion)),
            "base_service_ids": sorted(self.promotion_base_service_ids(promotion)),
            "starts_at": promotion.starts_at,
            "ends_at": promotion.ends_at,
            "name_uk": promotion.name_uk,
            "name_en": promotion.name_en,
            "description_uk": getattr(promotion, "description_uk", None),
            "description_en": getattr(promotion, "description_en", None),
            "discount_percent": promotion.discount_percent,
        }

    def should_show_in_public_catalog(self, promotion: Promotion) -> bool:
        return (
            self.is_automatic(promotion)
            or getattr(promotion, "applies_to_all_masters", True) is False
            or getattr(promotion, "applies_to_all_services", True) is False
        )

    def best_public_promotion(self, service: BarberService, promotions: Sequence[Promotion]) -> Promotion | None:
        applicable = [
            item
            for item in promotions
            if self.should_show_in_public_catalog(item) and self.applies_to_service(item, service)
        ]
        if not applicable:
            return None
        return max(applicable, key=lambda item: (item.discount_percent, -item.id))

    def annotate_public_promotions(
        self,
        services: Sequence[BarberService],
        promotions: Sequence[Promotion],
    ) -> None:
        for service in services:
            promotion = self.best_public_promotion(service, promotions)
            setattr(
                service,
                "active_promotion",
                self.public_promotion_payload(service, promotion) if promotion else None,
            )

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
            await session.execute(
                select(Promotion)
                .options(selectinload(Promotion.masters), selectinload(Promotion.base_services))
                .where(Promotion.code == normalized_code)
            )
        ).scalar_one_or_none()
        if promotion is None or not promotion.is_active or self.is_automatic(promotion):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion is not active")
        at = self._normalize_datetime(at)
        if promotion.starts_at is not None and self._normalize_datetime(promotion.starts_at) > at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion is not active yet")
        if promotion.ends_at is not None and self._normalize_datetime(promotion.ends_at) < at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promotion has expired")
        return promotion

    def is_automatic(self, promotion: Promotion) -> bool:
        return getattr(promotion, "application_mode", None) == PromotionApplicationMode.automatic

    async def lock_customer(self, session: AsyncSession, customer: Customer) -> Customer:
        # Serialize eligibility checks and completion on the canonical customer.
        with session.no_autoflush:
            locked = (await session.execute(
                select(Customer).where(or_(
                    Customer.id == customer.id,
                    Customer.phone.in_(phone_aliases(customer.phone)),
                )).order_by(Customer.id).with_for_update()
                .execution_options(populate_existing=True)
            )).scalars().all()
        return next(item for item in locked if item.id == customer.id)

    async def first_visit_reason(
        self, session: AsyncSession, customer: Customer | None, exclude_booking_id: int | None = None,
    ) -> str:
        if customer is None:
            return "customer_required"
        if (getattr(customer, "first_visit_completed_at", None) is not None
                or getattr(customer, "imported_last_visit_at", None) is not None
                or (getattr(customer, "imported_total_spent", None) or 0) > 0):
            return "not_eligible"
        aliases = phone_aliases(customer.phone) if getattr(customer, "phone", None) else set()
        customer_identity = or_(Customer.id == customer.id, Customer.phone.in_(aliases))
        alias_customer_ids = select(Customer.id).where(customer_identity)
        # Imported records may have used the local phone form. Do not merge their
        # accounts; any known visit evidence still disqualifies the same person.
        imported = select(Customer.id).where(customer_identity, or_(
            Customer.first_visit_completed_at.is_not(None),
            Customer.imported_last_visit_at.is_not(None),
            Customer.imported_total_spent > 0,
        )).limit(1)
        if (await session.execute(imported)).scalar_one_or_none() is not None:
            return "not_eligible"
        identity = or_(Booking.customer_id.in_(alias_customer_ids), Booking.customer_phone.in_(aliases))
        completed = select(Booking.id).where(identity, Booking.status == BookingStatus.completed).limit(1)
        if exclude_booking_id is not None:
            completed = completed.where(Booking.id != exclude_booking_id)
        if (await session.execute(completed)).scalar_one_or_none() is not None:
            return "not_eligible"
        reserved = select(Booking.id).where(Booking.first_visit_customer_id.in_(alias_customer_ids)).limit(1)
        if exclude_booking_id is not None:
            reserved = reserved.where(Booking.id != exclude_booking_id)
        if (await session.execute(reserved)).scalar_one_or_none() is not None:
            return "entitlement_reserved"
        return "eligible"

    async def apply_to_booking(
        self, session: AsyncSession, *, booking: Booking, promotion_code: str | None,
        customer: Customer | None, services: Sequence[BarberService], at: datetime,
        allow_private_promotions: bool = False, service_prices: Mapping[int, int] | None = None,
        preserve_existing: bool = False, reserve_entitlement: bool = True,
        apply_automatic: bool = True,
    ) -> None:
        subtotal = self.subtotal_amount(services, service_prices)
        manual = int(getattr(booking, "manual_discount_amount", 0) or 0) if preserve_existing else 0
        existing = preserve_existing and booking.promotion_discount_percent_snapshot is not None
        inactive = booking.status in (BookingStatus.cancelled, BookingStatus.no_show)
        promotion = None
        if existing:
            promotion = (await session.execute(select(Promotion).options(
                selectinload(Promotion.masters), selectinload(Promotion.base_services)
            ).where(Promotion.id == booking.promotion_id))).scalar_one_or_none() if booking.promotion_id else None
            if promotion is None:
                raise HTTPException(409, detail={"code": "promotion_unavailable", "message": "Select a promotion again before changing services"})
            self.ensure_snapshot_scope(promotion, services, at)
        elif promotion_code:
            if customer is None:
                raise HTTPException(400, detail="Booking customer is required for promotion")
            promotion = await self.get_active_by_code(session, promotion_code, at=at)
            if getattr(promotion, "is_public", True) is False and not allow_private_promotions:
                raise HTTPException(400, detail="Promotion is not available for public booking")
        elif apply_automatic:
            promotions = await self.list_active_public_catalog_promotions(session, at=at)
            candidates = [p for p in promotions if self.is_automatic(p)
                          and self.subtotal_amount(self.eligible_services(services, p), service_prices) > 0]
            if candidates and customer is not None:
                if reserve_entitlement:
                    customer = await self.lock_customer(session, customer)
                reason = await self.first_visit_reason(session, customer, booking.id)
                eligible_candidates = []
                for candidate in candidates:
                    if candidate.eligibility_type == PromotionEligibilityType.first_visit:
                        if reason == "eligible":
                            eligible_candidates.append(candidate)
                        continue
                    try:
                        await self.ensure_customer_eligible(session, promotion=candidate, customer=customer, at=at)
                    except HTTPException as exc:
                        if exc.status_code != 400:
                            raise
                    else:
                        eligible_candidates.append(candidate)
                candidates = eligible_candidates
                if candidates:
                    promotion = max(candidates, key=lambda p: (
                        self.discount_amount(self.subtotal_amount(self.eligible_services(services, p), service_prices), p), -p.id))

        first_visit = promotion is not None and (
            booking.promotion_eligibility_type_snapshot == PromotionEligibilityType.first_visit if existing
            else promotion.eligibility_type == PromotionEligibilityType.first_visit)
        consumed_snapshot = (existing and booking.status == BookingStatus.completed
                             and customer is not None and booking.first_visit_customer_id == customer.id)
        if first_visit and not (existing and inactive) and not consumed_snapshot:
            if customer is None:
                raise HTTPException(400, detail="Booking customer is required for promotion")
            if reserve_entitlement:
                customer = await self.lock_customer(session, customer)
            reason = await self.first_visit_reason(session, customer, booking.id)
            if reason != "eligible":
                raise HTTPException(409, detail={"code": reason, "message": "First-visit promotion is unavailable"})
        elif promotion is not None and not existing:
            await self.ensure_customer_eligible(session, promotion=promotion, customer=customer, at=at)

        booking.subtotal_amount = subtotal
        booking.manual_discount_amount = manual
        booking.first_visit_customer_id = customer.id if first_visit and reserve_entitlement and not inactive else None
        if promotion is None:
            for field in ("promotion_id", "promotion_code_snapshot", "promotion_name_uk_snapshot",
                          "promotion_name_en_snapshot", "promotion_discount_percent_snapshot",
                          "promotion_application_mode_snapshot", "promotion_eligibility_type_snapshot"):
                setattr(booking, field, None)
            booking.promotion_discount_amount = 0
        else:
            eligible_subtotal = self.subtotal_amount(self.eligible_services(services, promotion), service_prices)
            if eligible_subtotal <= 0:
                raise HTTPException(400, detail="Promotion does not apply to selected services")
            percent = booking.promotion_discount_percent_snapshot if existing else promotion.discount_percent
            booking.promotion_discount_amount = self.discount_amount(eligible_subtotal, SimpleNamespace(
                discount_type=PromotionDiscountType.percent, discount_percent=percent))
            if not existing:
                booking.promotion_id = promotion.id
                booking.promotion_code_snapshot = None if self.is_automatic(promotion) else promotion.code
                booking.promotion_name_uk_snapshot = promotion.name_uk
                booking.promotion_name_en_snapshot = promotion.name_en
                booking.promotion_discount_percent_snapshot = percent
                booking.promotion_application_mode_snapshot = getattr(promotion, "application_mode", None) or "code"
                booking.promotion_eligibility_type_snapshot = promotion.eligibility_type
        booking.total_amount = self.total_amount(subtotal, int(booking.promotion_discount_amount or 0) + manual)

    def ensure_snapshot_scope(self, promotion: Promotion, services: Sequence[BarberService], at: datetime) -> None:
        at = self._normalize_datetime(at)
        if ((promotion.starts_at is not None and self._normalize_datetime(promotion.starts_at) > at)
                or (promotion.ends_at is not None and self._normalize_datetime(promotion.ends_at) < at)
                or not self.eligible_services(services, promotion)):
            raise HTTPException(409, detail={"code": "promotion_no_longer_applicable", "message": "Promotion does not apply to the changed booking"})

    async def revalidate_first_visit_booking(
        self, session: AsyncSession, booking: Booking, *, check_scope: bool = True, reactivating: bool = False,
    ) -> None:
        if booking.promotion_eligibility_type_snapshot != PromotionEligibilityType.first_visit:
            return
        if not reactivating and booking.status in (BookingStatus.cancelled, BookingStatus.no_show):
            return
        if check_scope and booking.promotion_id:
            promotion = (await session.execute(select(Promotion).options(
                selectinload(Promotion.masters), selectinload(Promotion.base_services)
            ).where(Promotion.id == booking.promotion_id))).scalar_one_or_none()
            if promotion is not None:
                self.ensure_snapshot_scope(promotion, booking.services, booking.start_at)
        customer = await session.get(Customer, booking.customer_id)
        if customer is None:
            raise HTTPException(409, detail={"code": "customer_required", "message": "First-visit promotion requires a customer"})
        customer = await self.lock_customer(session, customer)
        reason = await self.first_visit_reason(session, customer, booking.id)
        if reason != "eligible":
            raise HTTPException(409, detail={"code": reason, "message": "First-visit promotion is unavailable"})
        booking.first_visit_customer_id = customer.id

    async def sync_first_visit_status(self, session: AsyncSession, booking: Booking, new_status: BookingStatus) -> None:
        if new_status in (BookingStatus.cancelled, BookingStatus.no_show):
            if not booking.first_visit_customer_id:
                return
            if booking.customer_id:
                customer = await session.get(Customer, booking.customer_id)
                if customer is not None:
                    await self.lock_customer(session, customer)
            booking.first_visit_customer_id = None
            return
        if new_status == BookingStatus.completed:
            customer = await session.get(Customer, booking.customer_id) if booking.customer_id else None
            if customer is not None:
                customer = await self.lock_customer(session, customer)
                if booking.promotion_eligibility_type_snapshot == PromotionEligibilityType.first_visit:
                    await self.revalidate_first_visit_booking(session, booking, check_scope=False, reactivating=True)
                customer.first_visit_completed_at = customer.first_visit_completed_at or booking.end_at
            return
        await self.revalidate_first_visit_booking(session, booking, reactivating=True)

    async def ensure_customer_eligible(
        self,
        session: AsyncSession,
        *,
        promotion: Promotion,
        customer: Customer,
        at: datetime,
    ) -> None:
        if promotion.eligibility_type == PromotionEligibilityType.first_visit:
            reason = await self.first_visit_reason(session, customer)
            if reason != "eligible":
                raise HTTPException(409, detail={"code": reason, "message": "First-visit promotion is unavailable"})
            return
        if promotion.eligibility_type == PromotionEligibilityType.all_customers:
            return
        if promotion.eligibility_type == PromotionEligibilityType.military_customers:
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
        application_mode = data.get("application_mode", promotion.application_mode)
        if (application_mode == PromotionApplicationMode.automatic
                and eligibility_type == PromotionEligibilityType.military_customers):
            raise HTTPException(400, detail="Military promotions require code application and verification")
        if eligibility_type == PromotionEligibilityType.inactive_customers and "inactive_days" not in data:
            data["inactive_days"] = promotion.inactive_days or 90
        if eligibility_type != PromotionEligibilityType.inactive_customers:
            data["inactive_days"] = None

        starts_at = data.get("starts_at", promotion.starts_at)
        ends_at = data.get("ends_at", promotion.ends_at)
        if (
            starts_at is not None
            and ends_at is not None
            and self._normalize_datetime(ends_at) <= self._normalize_datetime(starts_at)
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ends_at must be after starts_at")
        return data

    async def list_active_public_catalog_promotions(
        self,
        session: AsyncSession,
        *,
        at: datetime,
    ) -> Sequence[Promotion]:
        at = self._normalize_datetime(at)
        promotions = (
            await session.execute(
                select(Promotion)
                .options(selectinload(Promotion.masters), selectinload(Promotion.base_services))
                .where(
                    Promotion.is_active.is_(True),
                    Promotion.is_public.is_(True),
                    or_(Promotion.starts_at.is_(None), Promotion.starts_at <= at),
                    or_(Promotion.ends_at.is_(None), Promotion.ends_at >= at),
                )
            )
        ).scalars().all()
        return [item for item in promotions if self.should_show_in_public_catalog(item)]

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=KYIV_TZ)
        return value.astimezone(KYIV_TZ)
