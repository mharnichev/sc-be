from __future__ import annotations

import logging
from calendar import monthrange
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from types import MappingProxyType
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.booking import (
    BarberService,
    BaseService,
    Booking,
    BookingServiceItem,
    BookingSource,
    BookingStatus,
    Master,
    MasterAvailabilityWindow,
    MasterTimeBlock,
)
from app.models.customer import Customer
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus
from app.schemas.booking import AvailableSlotResponse, MasterAvailabilityWindowCreate, MasterTimeBlockCreate, PublicBookingCreate
from app.services.customer_auth import CustomerAuthService
from app.services.booking_funnel import BookingFunnelService
from app.services.promotion import PromotionService
from app.utils.booking_identity import canonical_booking_phone, phone_aliases

KYIV_TZ = ZoneInfo("Europe/Kyiv")
WORK_START = time(hour=9, minute=30)
WORK_END = time(hour=20)
SLOT_STEP_MINUTES = 15
CALENDAR_MASTER_BATCH_SIZE = 256
AVAILABILITY_HORIZON_MONTHS = 2
ACTIVE_BOOKING_STATUSES = (BookingStatus.confirmed,)
ACTIVE_WAITLIST_HOLD_STATUSES = (WaitlistOfferStatus.sent, WaitlistOfferStatus.delivered)
MONDAY = 0
CLOSED_WEEKDAYS = {MONDAY}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AvailableSlotsRange:
    booking_master_id: int
    duration_minutes: int
    catalog_price: Decimal
    slots_by_date: dict[date, list[AvailableSlotResponse]]
    has_slots_by_date: Mapping[date, bool] | None = None


CalendarInterval = tuple[datetime, datetime]


@dataclass(frozen=True)
class CalendarRangeData:
    """Request-local calendar facts, normalized and grouped by Kyiv calendar day.

    The maps intentionally contain only tuples and read-only mappings.  A caller
    may safely share one instance among synchronous slot calculations, but it is
    never cached or shared between requests/sessions.
    """

    availability_by_master_id: Mapping[int, Mapping[date, tuple[CalendarInterval, ...]]]
    busy_by_master_id: Mapping[int, Mapping[date, tuple[CalendarInterval, ...]]]


class BookingServiceLayer:
    def __init__(self) -> None:
        self.customer_auth_service = CustomerAuthService()
        self.booking_funnel_service = BookingFunnelService()
        self.promotion_service = PromotionService()

    async def add_booking_funnel_success_best_effort(
        self,
        session: AsyncSession,
        *,
        booking: Booking,
        requested_master: Master,
        anonymous_session_id: str | None,
    ) -> None:
        """Persist optional funnel telemetry without risking the booking transaction."""
        try:
            async with session.begin_nested():
                self.booking_funnel_service.add_booking_success(
                    session,
                    booking_id=booking.id,
                    master_id=requested_master.id,
                    service_id=booking.service_id,
                    anonymous_session_id=anonymous_session_id,
                )
                # Force analytics constraint/trigger failures into this savepoint.
                await session.flush()
        except Exception:
            logger.exception(
                "Booking funnel success telemetry failed; booking will still be committed booking_id=%s",
                booking.id,
            )

    def normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=KYIV_TZ)
        return value.astimezone(KYIV_TZ)

    def day_bounds(self, target_date: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(target_date, WORK_START, tzinfo=KYIV_TZ),
            datetime.combine(target_date, WORK_END, tzinfo=KYIV_TZ),
        )

    def add_calendar_months(self, value: date, months: int) -> date:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, monthrange(year, month)[1])
        return date(year, month, day)

    def availability_horizon_end_date(self) -> date:
        return self.add_calendar_months(datetime.now(KYIV_TZ).date(), AVAILABILITY_HORIZON_MONTHS)

    def is_closed_business_day(self, target_date: date) -> bool:
        return target_date.weekday() in CLOSED_WEEKDAYS

    def ensure_business_day_open(self, target_date: date) -> None:
        if self.is_closed_business_day(target_date):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Barbershop is closed on Mondays")

    def ensure_valid_interval(self, start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
        start_at = self.normalize_datetime(start_at)
        end_at = self.normalize_datetime(end_at)
        if start_at >= end_at:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start_at must be before end_at")
        return start_at, end_at

    def ensure_within_working_hours(self, start_at: datetime, end_at: datetime) -> None:
        start_at, end_at = self.ensure_valid_interval(start_at, end_at)
        self.ensure_business_day_open(start_at.date())
        day_start, day_end = self.day_bounds(start_at.date())
        if end_at.date() != start_at.date() or start_at < day_start or end_at > day_end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Booking must be within working hours 09:30-20:00 Europe/Kyiv",
            )

    def ensure_within_open_business_days(self, start_at: datetime, end_at: datetime) -> None:
        start_at, end_at = self.ensure_valid_interval(start_at, end_at)
        current_date = start_at.date()
        while current_date <= end_at.date():
            self.ensure_business_day_open(current_date)
            current_date += timedelta(days=1)

    def ensure_not_past(self, start_at: datetime) -> None:
        if self.normalize_datetime(start_at) <= datetime.now(KYIV_TZ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Slot is in the past")

    def ensure_availability_horizon(self, start_at: datetime, end_at: datetime) -> None:
        today = datetime.now(KYIV_TZ).date()
        horizon_end = self.availability_horizon_end_date()
        if start_at.date() < today or end_at.date() > horizon_end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Availability can only be opened within the next 2 months",
            )

    def ensure_valid_availability_window(self, start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
        start_at, end_at = self.ensure_valid_interval(start_at, end_at)
        self.ensure_within_working_hours(start_at, end_at)
        self.ensure_availability_horizon(start_at, end_at)
        return start_at, end_at

    def intervals_overlap(self, existing_start: datetime, existing_end: datetime, start_at: datetime, end_at: datetime) -> bool:
        return existing_start < end_at and existing_end > start_at

    async def get_active_master_with_services(
        self,
        session: AsyncSession,
        master_id: int,
        *,
        for_update: bool = False,
    ) -> Master:
        stmt = (
            select(Master)
            .options(selectinload(Master.services).selectinload(BarberService.base_service))
            .where(Master.id == master_id, Master.is_active.is_(True))
        )
        if for_update:
            stmt = stmt.with_for_update()
        master = (await session.execute(stmt)).scalar_one_or_none()
        if not master:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        return master

    async def resolve_booking_master(
        self,
        session: AsyncSession,
        master_id: int,
        *,
        for_update: bool = False,
    ) -> tuple[Master, Master]:
        if for_update:
            requested_master = await self.get_active_master_with_services(session, master_id, for_update=True)
        else:
            requested_master = await self.get_active_master_with_services(session, master_id)
        booking_master = requested_master
        visited_master_ids = {requested_master.id}

        while booking_redirect_master_id := getattr(booking_master, "booking_redirect_master_id", None):
            if booking_redirect_master_id in visited_master_ids:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking redirect cycle detected")
            visited_master_ids.add(booking_redirect_master_id)
            if for_update:
                booking_master = await self.get_active_master_with_services(
                    session,
                    booking_redirect_master_id,
                    for_update=True,
                )
            else:
                booking_master = await self.get_active_master_with_services(session, booking_redirect_master_id)

        return requested_master, booking_master

    async def get_active_service(self, session: AsyncSession, service_id: int) -> BarberService:
        stmt = (
            select(BarberService)
            .options(selectinload(BarberService.base_service))
            .where(BarberService.id == service_id, BarberService.is_active.is_(True))
        )
        service = (await session.execute(stmt)).scalar_one_or_none()
        base_service_id = getattr(service, "base_service_id", None)
        base_service = getattr(service, "base_service", None)
        if not service or (base_service_id is not None and not (base_service and base_service.is_active)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
        return service

    def ensure_master_provides_service(self, master: Master, service_id: int) -> None:
        if service_id not in {service.id for service in master.services}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Master does not provide this service")

    def ensure_master_provides_services(self, master: Master, service_ids: Sequence[int]) -> None:
        master_service_ids = {service.id for service in master.services}
        missing_service_ids = [service_id for service_id in service_ids if service_id not in master_service_ids]
        if missing_service_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Master does not provide this service")

    def resolve_duration_minutes(
        self,
        services: Sequence[BarberService],
        requested_duration_minutes: int | None,
        *,
        allow_override: bool = False,
    ) -> int:
        required_duration_minutes = sum(item.duration_minutes for item in services)
        if (
            requested_duration_minutes is not None
            and requested_duration_minutes != required_duration_minutes
            and not allow_override
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="duration_minutes must equal the selected services duration",
            )
        return requested_duration_minutes or required_duration_minutes

    def is_active_service(self, service: BarberService) -> bool:
        base_service = getattr(service, "base_service", None)
        return bool(getattr(service, "is_active", True)) and (
            getattr(service, "base_service_id", None) is None
            or base_service is None
            or bool(getattr(base_service, "is_active", True))
        )

    def custom_service_key(self, service: BarberService) -> tuple[str | None, str | None, int | None, int | None]:
        title_uk = getattr(service, "title_uk", None) or getattr(service, "name", None)
        title_en = getattr(service, "title_en", None)
        return (
            title_uk.strip().casefold() if title_uk else None,
            title_en.strip().casefold() if title_en else None,
            getattr(service, "duration_minutes", None),
            getattr(service, "price", None),
        )

    def find_redirect_service(self, booking_master: Master, source_service: BarberService) -> BarberService | None:
        active_booking_services = [
            item
            for item in getattr(booking_master, "services", [])
            if self.is_active_service(item)
        ]
        base_service_id = getattr(source_service, "base_service_id", None)
        if base_service_id is not None:
            return next(
                (
                    item
                    for item in active_booking_services
                    if getattr(item, "base_service_id", None) == base_service_id
                ),
                None,
            )

        source_key = self.custom_service_key(source_service)
        return next(
            (
                item
                for item in active_booking_services
                if getattr(item, "base_service_id", None) is None and self.custom_service_key(item) == source_key
            ),
            None,
        )

    async def resolve_booking_services_for_master(
        self,
        session: AsyncSession,
        requested_master: Master,
        booking_master: Master,
        service_ids: Sequence[int],
        source_services: Sequence[BarberService] | None = None,
    ) -> list[BarberService]:
        services = list(source_services) if source_services is not None else await self.get_active_services(
            session,
            service_ids,
        )
        if requested_master.id == booking_master.id:
            self.ensure_master_provides_services(booking_master, [item.id for item in services])
            return services

        requested_service_ids = {item.id for item in getattr(requested_master, "services", [])}
        booking_service_ids = {item.id for item in getattr(booking_master, "services", [])}
        resolved_services: list[BarberService] = []
        for item in services:
            if item.id in booking_service_ids:
                resolved_services.append(item)
                continue
            if item.id not in requested_service_ids:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Master does not provide this service")
            redirect_service = self.find_redirect_service(booking_master, item)
            resolved_services.append(redirect_service or item)

        resolved_service_ids = [item.id for item in resolved_services]
        if len(set(resolved_service_ids)) != len(resolved_service_ids):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="service_ids must not contain duplicates")
        return resolved_services

    async def get_active_services(self, session: AsyncSession, service_ids: Sequence[int]) -> list[BarberService]:
        services_by_id: dict[int, BarberService] = {}
        for service_id in service_ids:
            item = await self.get_active_service(session, service_id)
            services_by_id[item.id] = item
        return [services_by_id[service_id] for service_id in service_ids]

    def replace_booking_services(self, booking: Booking, services: Sequence[BarberService]) -> None:
        booking.service_id = services[0].id
        booking.service_items = [
            BookingServiceItem(
                service_id=item.id,
                position=index,
                price_amount=int(getattr(item, "price", 0) or 0),
            )
            for index, item in enumerate(services)
        ]

    async def update_booking_services(
        self,
        session: AsyncSession,
        booking: Booking,
        services: Sequence[BarberService],
        service_prices: dict[int, int] | None = None,
    ) -> None:
        booking.service_id = services[0].id
        await session.execute(delete(BookingServiceItem).where(BookingServiceItem.booking_id == booking.id))
        for index, item in enumerate(services):
            session.add(
                BookingServiceItem(
                    booking_id=booking.id,
                    service_id=item.id,
                    position=index,
                    price_amount=int((service_prices or {}).get(item.id, getattr(item, "price", 0) or 0)),
                )
            )

    async def copy_active_base_services_to_master(self, session: AsyncSession, master: Master) -> list[BarberService]:
        return await self.copy_active_base_services_to_master_id(session, master.id)

    async def copy_active_base_services_to_master_id(self, session: AsyncSession, master_id: int) -> list[BarberService]:
        base_services = (
            await session.execute(select(BaseService).where(BaseService.is_active.is_(True)).order_by(BaseService.id.asc()))
        ).scalars().all()
        if not base_services:
            return []

        existing_base_ids = {
            base_service_id
            for base_service_id in (
                await session.execute(
                    select(BarberService.base_service_id).where(
                        BarberService.master_id == master_id,
                        BarberService.base_service_id.is_not(None),
                    )
                )
            ).scalars().all()
        }
        copied: list[BarberService] = []
        for base_service in base_services:
            if base_service.id in existing_base_ids:
                continue
            barber_service = BarberService(
                master_id=master_id,
                base_service_id=base_service.id,
                name=base_service.name,
                title_uk=getattr(base_service, "title_uk", None) or base_service.name,
                title_en=getattr(base_service, "title_en", None),
                description=base_service.description,
                description_uk=getattr(base_service, "description_uk", None) or base_service.description,
                description_en=getattr(base_service, "description_en", None),
                duration_minutes=base_service.duration_minutes,
                price=base_service.price,
                is_active=base_service.is_active,
            )
            session.add(barber_service)
            copied.append(barber_service)
        if copied:
            await session.flush()
        return copied

    async def sync_default_services_for_barber(self, session: AsyncSession, barber_id: int) -> int:
        copied = await self.copy_active_base_services_to_master_id(session, barber_id)
        return len(copied)

    async def list_busy_bookings(
        self,
        session: AsyncSession,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
        exclude_booking_id: int | None = None,
        exclude_waitlist_offer_id: int | None = None,
    ) -> Sequence[Booking | WaitlistOffer]:
        stmt = select(Booking).where(
            Booking.master_id == master_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.start_at < end_at,
            Booking.end_at > start_at,
        )
        if exclude_booking_id is not None:
            stmt = stmt.where(Booking.id != exclude_booking_id)
        bookings = list((await session.execute(stmt)).scalars().all())
        raw_offers = await self.list_active_waitlist_holds(
            session,
            master_id=master_id,
            start_at=start_at,
            end_at=end_at,
            exclude_waitlist_offer_id=exclude_waitlist_offer_id,
        )
        # A few legacy unit-test sessions return a scalar sentinel once their
        # programmed results are exhausted. Real SQLAlchemy always returns a
        # sequence here; treating a non-sequence sentinel as empty preserves the
        # test-double contract without weakening the production query.
        offers = list(raw_offers) if isinstance(raw_offers, Sequence) else []
        return [*bookings, *offers]

    async def list_active_waitlist_holds(
        self,
        session: AsyncSession,
        *,
        master_id: int | None,
        start_at: datetime,
        end_at: datetime,
        exclude_waitlist_offer_id: int | None = None,
    ) -> Sequence[WaitlistOffer]:
        stmt = select(WaitlistOffer).where(
            WaitlistOffer.status.in_(ACTIVE_WAITLIST_HOLD_STATUSES),
            WaitlistOffer.expires_at > datetime.now(KYIV_TZ),
            WaitlistOffer.start_at < end_at,
            WaitlistOffer.end_at > start_at,
        )
        if master_id is not None:
            stmt = stmt.where(WaitlistOffer.master_id == master_id)
        if exclude_waitlist_offer_id is not None:
            stmt = stmt.where(WaitlistOffer.id != exclude_waitlist_offer_id)
        return (await session.execute(stmt)).scalars().all()

    async def list_time_blocks(
        self,
        session: AsyncSession,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> Sequence[MasterTimeBlock]:
        stmt = select(MasterTimeBlock).where(
            MasterTimeBlock.master_id == master_id,
            MasterTimeBlock.start_at < end_at,
            MasterTimeBlock.end_at > start_at,
        )
        return (await session.execute(stmt)).scalars().all()

    async def list_availability_windows(
        self,
        session: AsyncSession,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> Sequence[MasterAvailabilityWindow]:
        stmt = (
            select(MasterAvailabilityWindow)
            .where(
                MasterAvailabilityWindow.master_id == master_id,
                MasterAvailabilityWindow.start_at < end_at,
                MasterAvailabilityWindow.end_at > start_at,
            )
            .order_by(MasterAvailabilityWindow.start_at.asc())
        )
        return (await session.execute(stmt)).scalars().all()

    async def ensure_no_overlapping_availability(
        self,
        session: AsyncSession,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> None:
        existing = (
            await session.execute(
                select(MasterAvailabilityWindow.id)
                .where(
                    MasterAvailabilityWindow.master_id == master_id,
                    MasterAvailabilityWindow.start_at < end_at,
                    MasterAvailabilityWindow.end_at > start_at,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Availability window overlaps an existing window")

    async def ensure_booking_within_availability(
        self,
        session: AsyncSession,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> None:
        start_at, end_at = self.ensure_valid_interval(start_at, end_at)
        self.ensure_within_working_hours(start_at, end_at)
        self.ensure_availability_horizon(start_at, end_at)
        windows = await self.list_availability_windows(session, master_id, start_at, end_at)
        available = self._availability_intervals(start_at.date(), windows)
        if not any(left <= start_at and right >= end_at for left, right in available):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Booking slot is outside master's open availability",
            )

    async def get_available_slots(
        self,
        session: AsyncSession,
        master_id: int,
        service_id: int | None,
        target_date: date,
        service_ids: Sequence[int] | None = None,
        duration_minutes: int | None = None,
    ) -> list[AvailableSlotResponse]:
        result = await self.get_available_slots_for_range(
            session,
            master_id,
            target_date,
            target_date,
            service_id=service_id,
            service_ids=service_ids,
            duration_minutes=duration_minutes,
        )
        return result.slots_by_date[target_date]

    def _group_calendar_intervals_by_date(
        self,
        intervals: Sequence[CalendarInterval],
        *,
        start_date: date,
        end_date: date,
    ) -> Mapping[date, tuple[CalendarInterval, ...]]:
        """Group intervals only on Kyiv working days they actually intersect."""
        grouped: dict[date, list[CalendarInterval]] = {}
        for raw_start, raw_end in intervals:
            start = self.normalize_datetime(raw_start)
            end = self.normalize_datetime(raw_end)
            if start >= end:
                continue
            current_date = max(start.date(), start_date)
            last_date = min(end.date(), end_date)
            while current_date <= last_date:
                day_start, day_end = self.day_bounds(current_date)
                if start < day_end and end > day_start:
                    grouped.setdefault(current_date, []).append((start, end))
                current_date += timedelta(days=1)
        return MappingProxyType({
            item_date: tuple(sorted(day_intervals))
            for item_date, day_intervals in grouped.items()
        })

    def _calendar_range_data(
        self,
        *,
        calendar_master_ids: Sequence[int],
        start_date: date,
        end_date: date,
        availability_windows: Sequence[MasterAvailabilityWindow],
        bookings: Sequence[Booking | WaitlistOffer],
        blocks: Sequence[MasterTimeBlock],
    ) -> CalendarRangeData:
        master_ids = tuple(dict.fromkeys(calendar_master_ids))
        availability_raw: dict[int, list[CalendarInterval]] = {master_id: [] for master_id in master_ids}
        busy_raw: dict[int, list[CalendarInterval]] = {master_id: [] for master_id in master_ids}
        default_master_id = master_ids[0] if len(master_ids) == 1 else None
        for window in availability_windows:
            window_master_id = getattr(window, "master_id", default_master_id)
            if window_master_id in availability_raw:
                availability_raw[window_master_id].append((window.start_at, window.end_at))
        for item in [*bookings, *blocks]:
            item_master_id = getattr(item, "master_id", default_master_id)
            if item_master_id in busy_raw:
                busy_raw[item_master_id].append((item.start_at, item.end_at))
        return CalendarRangeData(
            availability_by_master_id=MappingProxyType({
                master_id: self._group_calendar_intervals_by_date(
                    intervals, start_date=start_date, end_date=end_date,
                )
                for master_id, intervals in availability_raw.items()
            }),
            busy_by_master_id=MappingProxyType({
                master_id: self._group_calendar_intervals_by_date(
                    intervals, start_date=start_date, end_date=end_date,
                )
                for master_id, intervals in busy_raw.items()
            }),
        )

    async def load_calendar_range_data(
        self,
        session: AsyncSession,
        calendar_master_ids: Sequence[int],
        start_at: datetime,
        end_at: datetime,
    ) -> CalendarRangeData:
        """Bulk-load calendar facts for canonical masters in four bounded queries."""
        start_at, end_at = self.ensure_valid_interval(start_at, end_at)
        master_ids = tuple(dict.fromkeys(calendar_master_ids))
        if not master_ids:
            return CalendarRangeData(MappingProxyType({}), MappingProxyType({}))

        availability_windows: list[MasterAvailabilityWindow] = []
        bookings: list[Booking] = []
        holds: list[WaitlistOffer] = []
        blocks: list[MasterTimeBlock] = []
        now = datetime.now(KYIV_TZ)
        for offset in range(0, len(master_ids), CALENDAR_MASTER_BATCH_SIZE):
            master_id_batch = master_ids[offset:offset + CALENDAR_MASTER_BATCH_SIZE]
            availability_stmt = (
                select(MasterAvailabilityWindow)
                .where(
                    MasterAvailabilityWindow.master_id.in_(master_id_batch),
                    MasterAvailabilityWindow.start_at < end_at,
                    MasterAvailabilityWindow.end_at > start_at,
                )
                .order_by(MasterAvailabilityWindow.master_id.asc(), MasterAvailabilityWindow.start_at.asc())
            )
            bookings_stmt = select(Booking).where(
                Booking.master_id.in_(master_id_batch),
                Booking.status.in_(ACTIVE_BOOKING_STATUSES),
                Booking.start_at < end_at,
                Booking.end_at > start_at,
            )
            holds_stmt = select(WaitlistOffer).where(
                WaitlistOffer.master_id.in_(master_id_batch),
                WaitlistOffer.status.in_(ACTIVE_WAITLIST_HOLD_STATUSES),
                WaitlistOffer.expires_at > now,
                WaitlistOffer.start_at < end_at,
                WaitlistOffer.end_at > start_at,
            )
            blocks_stmt = select(MasterTimeBlock).where(
                MasterTimeBlock.master_id.in_(master_id_batch),
                MasterTimeBlock.start_at < end_at,
                MasterTimeBlock.end_at > start_at,
            )
            availability_windows.extend((await session.execute(availability_stmt)).scalars().all())
            bookings.extend((await session.execute(bookings_stmt)).scalars().all())
            holds.extend((await session.execute(holds_stmt)).scalars().all())
            blocks.extend((await session.execute(blocks_stmt)).scalars().all())
        return self._calendar_range_data(
            calendar_master_ids=master_ids,
            start_date=start_at.date(),
            end_date=end_at.date(),
            availability_windows=availability_windows,
            bookings=[*bookings, *holds],
            blocks=blocks,
        )

    def _merge_intervals(
        self, intervals: Sequence[tuple[datetime, datetime]],
    ) -> list[tuple[datetime, datetime]]:
        """Union half-open intervals, including touching boundaries."""
        merged: list[tuple[datetime, datetime]] = []
        for start, end in sorted(intervals):
            if start >= end:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def _availability_intervals(
        self, target_date: date, windows: Sequence[MasterAvailabilityWindow],
    ) -> list[tuple[datetime, datetime]]:
        """Callers load one physical calendar; never join across a working day."""
        return self._availability_intervals_from_pairs(
            target_date,
            [(window.start_at, window.end_at) for window in windows],
        )

    def _availability_intervals_from_pairs(
        self, target_date: date, windows: Sequence[CalendarInterval],
    ) -> list[CalendarInterval]:
        """Clip and merge published intervals for one Kyiv working day."""
        if self.is_closed_business_day(target_date):
            return []
        day_start, day_end = self.day_bounds(target_date)
        return self._merge_intervals([
            (max(self.normalize_datetime(start), day_start), min(self.normalize_datetime(end), day_end))
            for start, end in windows
        ])

    def _free_calendar_intervals(
        self,
        *,
        target_date: date,
        availability_intervals: Sequence[CalendarInterval],
        busy_intervals: Sequence[CalendarInterval],
    ) -> list[CalendarInterval]:
        available = self._availability_intervals_from_pairs(target_date, availability_intervals)
        occupied = self._merge_intervals([
            (self.normalize_datetime(start), self.normalize_datetime(end))
            for start, end in busy_intervals
        ])
        free: list[CalendarInterval] = []
        for start, end in available:
            cursor = start
            for busy_start, busy_end in occupied:
                if busy_end <= cursor:
                    continue
                if busy_start >= end:
                    break
                if busy_start > cursor:
                    free.append((cursor, busy_start))
                cursor = max(cursor, busy_end)
                if cursor >= end:
                    break
            if cursor < end:
                free.append((cursor, end))
        return free

    def _calendar_slot_starts(
        self,
        *,
        target_date: date,
        duration: timedelta,
        availability_intervals: Sequence[CalendarInterval],
        busy_intervals: Sequence[CalendarInterval],
        stop_after_first: bool = False,
    ) -> list[datetime]:
        """Return allowed starts using the shared published-window/off-grid policy."""
        free = self._free_calendar_intervals(
            target_date=target_date,
            availability_intervals=availability_intervals,
            busy_intervals=busy_intervals,
        )
        now = datetime.now(KYIV_TZ)
        day_start, _ = self.day_bounds(target_date)
        step = timedelta(minutes=SLOT_STEP_MINUTES)
        starts: set[datetime] = set()
        for start, end in free:
            if start > now and start + duration <= end:
                if stop_after_first:
                    return [start]
                starts.add(start)
            # Anchor the grid to the working day, never to the end of a block.
            current = day_start + ((start - day_start) // step) * step
            if current < start:
                current += step
            while current + duration <= end:
                if current > now:
                    if stop_after_first:
                        return [current]
                    starts.add(current)
                current += step
        return sorted(starts)

    def _slots_from_calendar(
        self,
        *,
        target_date: date,
        duration: timedelta,
        availability_windows: Sequence[MasterAvailabilityWindow],
        busy_intervals: Sequence[tuple[datetime, datetime]],
    ) -> list[AvailableSlotResponse]:
        """Subtract occupied unions, then offer free starts and the fixed 15-minute grid."""
        starts = self._calendar_slot_starts(
            target_date=target_date,
            duration=duration,
            availability_intervals=[(window.start_at, window.end_at) for window in availability_windows],
            busy_intervals=busy_intervals,
        )
        return [AvailableSlotResponse(start_at=start, end_at=start + duration) for start in starts]

    def iter_calendar_slots(
        self,
        data: CalendarRangeData,
        *,
        calendar_master_id: int,
        start_date: date,
        end_date: date,
        duration: timedelta,
    ) -> Iterator[tuple[date, datetime, datetime]]:
        """Lazily yield slot pairs from one bulk calendar load without DTO allocation."""
        availability = data.availability_by_master_id.get(calendar_master_id, {})
        busy = data.busy_by_master_id.get(calendar_master_id, {})
        current_date = start_date
        while current_date <= end_date:
            for start in self._calendar_slot_starts(
                target_date=current_date,
                duration=duration,
                availability_intervals=availability.get(current_date, ()),
                busy_intervals=busy.get(current_date, ()),
            ):
                yield current_date, start, start + duration
            current_date += timedelta(days=1)

    def calendar_slot_exists(
        self,
        data: CalendarRangeData,
        *,
        calendar_master_id: int,
        target_date: date,
        duration: timedelta,
    ) -> bool:
        """Answer a date-picker existence check without materializing slots or DTOs."""
        availability = data.availability_by_master_id.get(calendar_master_id, {})
        busy = data.busy_by_master_id.get(calendar_master_id, {})
        return bool(self._calendar_slot_starts(
            target_date=target_date,
            duration=duration,
            availability_intervals=availability.get(target_date, ()),
            busy_intervals=busy.get(target_date, ()),
            stop_after_first=True,
        ))

    async def get_available_slots_for_range(
        self,
        session: AsyncSession,
        master_id: int,
        start_date: date,
        end_date: date,
        *,
        service_id: int | None = None,
        service_ids: Sequence[int] | None = None,
        duration_minutes: int | None = None,
        exists_only: bool = False,
    ) -> AvailableSlotsRange:
        """Load a public booking context and its calendar once for an inclusive date range."""
        if start_date > end_date:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start_date must not be after end_date")
        today, horizon_end = datetime.now(KYIV_TZ).date(), self.availability_horizon_end_date()
        if start_date < today or end_date > horizon_end:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date range is outside availability horizon")

        selected_service_ids = list(service_ids or ([] if service_id is None else [service_id]))
        if not selected_service_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="service_id or service_ids is required")
        if len(set(selected_service_ids)) != len(selected_service_ids):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="service_ids must not contain duplicates")
        requested_master, booking_master = await self.resolve_booking_master(session, master_id)
        if not bool(getattr(requested_master, "show_on_master_block", True)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        source_services = await self.get_active_services(session, selected_service_ids)
        self.ensure_master_provides_services(requested_master, selected_service_ids)
        services = await self.resolve_booking_services_for_master(
            session,
            requested_master,
            booking_master,
            selected_service_ids,
            source_services=source_services,
        )
        duration = timedelta(minutes=self.resolve_duration_minutes(source_services, duration_minutes))
        requested_dates: list[date] = []
        current_date = start_date
        while current_date <= end_date:
            requested_dates.append(current_date)
            current_date += timedelta(days=1)
        slots_by_date: dict[date, list[AvailableSlotResponse]] = {item_date: [] for item_date in requested_dates}
        has_slots_by_date: dict[date, bool] | None = (
            {item_date: False for item_date in requested_dates} if exists_only else None
        )
        open_dates = [item_date for item_date in requested_dates if not self.is_closed_business_day(item_date)]
        if open_dates:
            # Keep the long-standing one-calendar hooks here for consumers that
            # specialize them; multi-master callers use load_calendar_range_data.
            range_start, _ = self.day_bounds(open_dates[0])
            _, range_end = self.day_bounds(open_dates[-1])
            availability_windows = await self.list_availability_windows(
                session, booking_master.id, range_start, range_end
            )
            bookings = await self.list_busy_bookings(session, booking_master.id, range_start, range_end)
            blocks = await self.list_time_blocks(session, booking_master.id, range_start, range_end)
            calendar_data = self._calendar_range_data(
                calendar_master_ids=[booking_master.id],
                start_date=open_dates[0],
                end_date=open_dates[-1],
                availability_windows=availability_windows,
                bookings=bookings,
                blocks=blocks,
            )
            for item_date in open_dates:
                if exists_only:
                    has_slots_by_date[item_date] = self.calendar_slot_exists(
                        calendar_data,
                        calendar_master_id=booking_master.id,
                        target_date=item_date,
                        duration=duration,
                    )
                    continue
                availability = calendar_data.availability_by_master_id[booking_master.id]
                busy = calendar_data.busy_by_master_id[booking_master.id]
                starts = self._calendar_slot_starts(
                    target_date=item_date,
                    duration=duration,
                    availability_intervals=availability.get(item_date, ()),
                    busy_intervals=busy.get(item_date, ()),
                )
                slots_by_date[item_date] = [
                    AvailableSlotResponse(start_at=start, end_at=start + duration) for start in starts
                ]
        return AvailableSlotsRange(
            booking_master_id=booking_master.id,
            duration_minutes=int(duration.total_seconds() // 60),
            catalog_price=sum((Decimal(str(getattr(item, "price", 0) or 0)) for item in services), Decimal(0)),
            slots_by_date=slots_by_date,
            has_slots_by_date=has_slots_by_date,
        )

    async def get_available_slots_range(
        self,
        session: AsyncSession,
        master_id: int,
        start_date: date,
        end_date: date,
        *,
        service_id: int | None = None,
        service_ids: Sequence[int] | None = None,
        duration_minutes: int | None = None,
    ) -> list[tuple[date, bool]]:
        result = await self.get_available_slots_for_range(
            session,
            master_id,
            start_date,
            end_date,
            service_id=service_id,
            service_ids=service_ids,
            duration_minutes=duration_minutes,
            exists_only=True,
        )
        return [
            (item_date, result.has_slots_by_date[item_date])
            for item_date in result.slots_by_date
        ]

    async def ensure_slot_available(
        self,
        session: AsyncSession,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
        exclude_booking_id: int | None = None,
        exclude_waitlist_offer_id: int | None = None,
    ) -> None:
        # Serialize every production writer for a master before the conflict
        # query. This covers normal booking, rescheduling, admin edits and
        # waitlist claims with the same database lock. Lightweight unit-test
        # sessions are intentionally not mistaken for a real AsyncSession.
        if isinstance(session, AsyncSession):
            await session.execute(
                select(Master.id).where(Master.id == master_id).with_for_update()
            )
        if exclude_waitlist_offer_id is None:
            bookings = await self.list_busy_bookings(
                session,
                master_id,
                start_at,
                end_at,
                exclude_booking_id,
            )
        else:
            # Claiming a hold is the only path that may exclude one offer. Call
            # the concrete implementation so test doubles with the legacy
            # signature remain compatible for every ordinary booking path.
            bookings = await BookingServiceLayer.list_busy_bookings(
                self,
                session,
                master_id,
                start_at,
                end_at,
                exclude_booking_id,
                exclude_waitlist_offer_id,
            )
        if bookings:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Booking slot overlaps an existing booking")
        blocks = await self.list_time_blocks(session, master_id, start_at, end_at)
        if blocks:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Booking slot overlaps a blocked interval")

    def split_customer_name(self, full_name: str) -> tuple[str, str | None]:
        parts = full_name.strip().split(maxsplit=1)
        if not parts:
            return full_name, None
        return parts[0], parts[1] if len(parts) > 1 else None

    def normalize_customer_phone(self, phone: str) -> str:
        return canonical_booking_phone(self.customer_auth_service.normalize_phone(phone))

    async def find_booking_customer(
        self, session: AsyncSession, *, phone: str | None, email: str | None,
    ) -> Customer | None:
        customer = None
        if phone:
            customer = (await session.execute(
                select(Customer).where(Customer.phone.in_(phone_aliases(phone)))
                .order_by(Customer.phone != phone, Customer.id).limit(1)
            )).scalar_one_or_none()
        if customer is None and email:
            customer = (await session.execute(
                select(Customer).where(Customer.email == email.lower())
            )).scalar_one_or_none()
        return customer

    async def get_or_create_booking_customer(
        self,
        session: AsyncSession,
        payload: PublicBookingCreate,
    ) -> tuple[Customer, str]:
        normalized_phone = self.normalize_customer_phone(payload.customer_phone)
        submitted_phone = self.customer_auth_service.normalize_phone(payload.customer_phone)
        email = str(payload.customer_email).lower() if payload.customer_email else None

        # A row lock cannot protect a customer that does not exist yet. Serialize
        # identity creation before lookup, including the existing email fallback.
        bind = getattr(session, "bind", None)
        if bind is not None and bind.dialect.name == "postgresql":
            identities = [f"booking-phone:{normalized_phone}"]
            if email:
                identities.append(f"booking-email:{email}")
            for identity in sorted(identities):
                key = int.from_bytes(sha256(identity.encode()).digest()[:8], "big", signed=True)
                await session.execute(select(func.pg_advisory_xact_lock(key)))

        customer = await self.find_booking_customer(session, phone=submitted_phone, email=email)

        if customer is None:
            name, surname = self.split_customer_name(payload.customer_name)
            customer = Customer(
                phone=normalized_phone,
                email=email,
                name=name,
                surname=surname,
                is_active=True,
            )
            session.add(customer)
            await session.flush()
            return customer, normalized_phone

        # Enrichment must not lock a higher-ID alias before eligibility/completion
        # acquires the same customer rows in ascending ID order.
        customer = await self.promotion_service.lock_customer(session, customer)

        if email and customer.email is None:
            existing_email_owner = (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one_or_none()
            if existing_email_owner is not None and existing_email_owner.id != customer.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already in use")
            customer.email = email

        if not customer.name:
            customer.name, customer.surname = self.split_customer_name(payload.customer_name)

        await session.flush()
        return customer, normalized_phone

    async def create_public_booking(
        self,
        session: AsyncSession,
        payload: PublicBookingCreate,
        *,
        promotion_code: str | None = None,
        allow_private_promotions: bool = False,
        allow_past: bool = False,
        require_availability: bool = True,
        require_working_hours: bool = True,
        record_funnel_success: bool = False,
        source: BookingSource = BookingSource.unknown,
        allow_duration_override: bool = False,
        repeat_booking_token: str | None = None,
    ) -> Booking:
        start_at = self.normalize_datetime(payload.start_at)
        if not allow_past:
            self.ensure_not_past(start_at)

        try:
            requested_master, booking_master = await self.resolve_booking_master(
                session,
                payload.master_id,
                for_update=True,
            )
            selected_service_ids = payload.service_ids or [payload.service_id]
            source_services = await self.get_active_services(session, selected_service_ids)
            self.ensure_master_provides_services(requested_master, selected_service_ids)
            services = await self.resolve_booking_services_for_master(
                session,
                requested_master,
                booking_master,
                selected_service_ids,
                source_services=source_services,
            )

            duration_minutes = self.resolve_duration_minutes(
                source_services,
                payload.duration_minutes,
                allow_override=allow_duration_override,
            )
            end_at = start_at + timedelta(minutes=duration_minutes)
            if require_working_hours:
                self.ensure_within_working_hours(start_at, end_at)
            else:
                self.ensure_within_open_business_days(start_at, end_at)
            if require_availability:
                await self.ensure_booking_within_availability(session, booking_master.id, start_at, end_at)
            await self.ensure_slot_available(session, booking_master.id, start_at, end_at)
            customer, customer_phone = await self.get_or_create_booking_customer(session, payload)

            booking = Booking(
                master_id=booking_master.id,
                service_id=services[0].id,
                customer_id=customer.id,
                redirected_from_master_id=(
                    requested_master.id
                    if requested_master.id != booking_master.id
                    else None
                ),
                customer_name=payload.customer_name,
                customer_phone=customer_phone,
                customer_email=customer.email,
                customer_comment=payload.customer_comment,
                start_at=start_at,
                end_at=end_at,
                status=BookingStatus.confirmed,
                source=source,
            )
            self.replace_booking_services(booking, services)
            await self.promotion_service.apply_to_booking(
                session,
                booking=booking,
                promotion_code=promotion_code,
                offer_token=getattr(payload, "offer_token", None),
                customer=customer,
                services=services,
                at=start_at,
                allow_private_promotions=allow_private_promotions,
            )
            if payload.expected_total_amount is not None and payload.expected_total_amount != booking.total_amount:
                raise HTTPException(status_code=409, detail={
                    "code": "price_changed",
                    "message": "The booking price changed. Request a new quote and approve it before retrying.",
                    "total_amount": booking.total_amount,
                })
            session.add(booking)
            await session.flush()
            if record_funnel_success:
                await self.add_booking_funnel_success_best_effort(
                    session,
                    booking=booking,
                    requested_master=requested_master,
                    anonymous_session_id=payload.funnel_session_id,
                )
            if repeat_booking_token:
                # Local import avoids coupling the base booking module to the
                # optional lifecycle service at import time.
                from app.services.repeat_booking import repeat_booking_service

                await repeat_booking_service.attribute_booking(
                    session,
                    token=repeat_booking_token,
                    booking=booking,
                    requested_master_id=requested_master.id,
                    requested_service_ids=list(selected_service_ids),
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise

        await session.refresh(booking)
        return booking

    async def create_availability_window(
        self,
        session: AsyncSession,
        master: Master,
        payload: MasterAvailabilityWindowCreate,
    ) -> MasterAvailabilityWindow:
        start_at, end_at = self.ensure_valid_availability_window(payload.start_at, payload.end_at)
        try:
            await self.ensure_no_overlapping_availability(session, master.id, start_at, end_at)
            window = MasterAvailabilityWindow(master_id=master.id, start_at=start_at, end_at=end_at)
            session.add(window)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(window)
        return window

    async def create_availability_days(
        self,
        session: AsyncSession,
        master: Master,
        dates: Sequence[date],
    ) -> list[MasterAvailabilityWindow]:
        windows: list[MasterAvailabilityWindow] = []
        intervals: list[tuple[datetime, datetime]] = []
        try:
            for target_date in dates:
                start_at, end_at = self.day_bounds(target_date)
                start_at, end_at = self.ensure_valid_availability_window(start_at, end_at)
                if any(
                    self.intervals_overlap(existing_start, existing_end, start_at, end_at)
                    for existing_start, existing_end in intervals
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Availability window overlaps an existing window",
                    )
                await self.ensure_no_overlapping_availability(session, master.id, start_at, end_at)
                intervals.append((start_at, end_at))
                window = MasterAvailabilityWindow(master_id=master.id, start_at=start_at, end_at=end_at)
                session.add(window)
                windows.append(window)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        for window in windows:
            await session.refresh(window)
        return windows

    async def delete_availability_window(
        self,
        session: AsyncSession,
        window: MasterAvailabilityWindow,
        *,
        allow_booked: bool = False,
    ) -> None:
        if not allow_booked:
            bookings = await self.list_busy_bookings(session, window.master_id, window.start_at, window.end_at)
            if bookings:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Availability window has active bookings")
        await session.delete(window)
        await session.commit()

    async def create_time_block(
        self,
        session: AsyncSession,
        master: Master,
        payload: MasterTimeBlockCreate,
    ) -> MasterTimeBlock:
        start_at, end_at = self.ensure_valid_interval(payload.start_at, payload.end_at)
        block = MasterTimeBlock(master_id=master.id, start_at=start_at, end_at=end_at, reason=payload.reason)
        session.add(block)
        await session.commit()
        await session.refresh(block)
        return block


async def sync_default_services_for_barber(session: AsyncSession, barber_id: int) -> int:
    return await BookingServiceLayer().sync_default_services_for_barber(session, barber_id)
