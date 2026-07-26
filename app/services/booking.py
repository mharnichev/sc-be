from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.booking import (
    BarberService,
    BaseService,
    Booking,
    BookingServiceItem,
    BookingStatus,
    Master,
    MasterAvailabilityWindow,
    MasterTimeBlock,
)
from app.models.customer import Customer
from app.schemas.booking import AvailableSlotResponse, MasterAvailabilityWindowCreate, MasterTimeBlockCreate, PublicBookingCreate
from app.services.customer_auth import CustomerAuthService
from app.services.booking_funnel import BookingFunnelService
from app.services.promotion import PromotionService

KYIV_TZ = ZoneInfo("Europe/Kyiv")
WORK_START = time(hour=8)
WORK_END = time(hour=20)
SLOT_STEP_MINUTES = 15
AVAILABILITY_HORIZON_MONTHS = 2
ACTIVE_BOOKING_STATUSES = (BookingStatus.confirmed,)
MONDAY = 0
CLOSED_WEEKDAYS = {MONDAY}


class BookingServiceLayer:
    def __init__(self) -> None:
        self.customer_auth_service = CustomerAuthService()
        self.booking_funnel_service = BookingFunnelService()
        self.promotion_service = PromotionService()

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
                detail="Booking must be within working hours 08:00-20:00 Europe/Kyiv",
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
    ) -> list[BarberService]:
        services = await self.get_active_services(session, service_ids)
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
            BookingServiceItem(service_id=item.id, position=index)
            for index, item in enumerate(services)
        ]

    async def update_booking_services(
        self,
        session: AsyncSession,
        booking: Booking,
        services: Sequence[BarberService],
    ) -> None:
        booking.service_id = services[0].id
        await session.execute(delete(BookingServiceItem).where(BookingServiceItem.booking_id == booking.id))
        for index, item in enumerate(services):
            session.add(BookingServiceItem(booking_id=booking.id, service_id=item.id, position=index))

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
    ) -> Sequence[Booking]:
        stmt = select(Booking).where(
            Booking.master_id == master_id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.start_at < end_at,
            Booking.end_at > start_at,
        )
        if exclude_booking_id is not None:
            stmt = stmt.where(Booking.id != exclude_booking_id)
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
        available = (
            await session.execute(
                select(MasterAvailabilityWindow.id)
                .where(
                    MasterAvailabilityWindow.master_id == master_id,
                    MasterAvailabilityWindow.start_at <= start_at,
                    MasterAvailabilityWindow.end_at >= end_at,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if available is None:
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
        selected_service_ids = list(service_ids or ([] if service_id is None else [service_id]))
        if not selected_service_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="service_id or service_ids is required")
        if len(set(selected_service_ids)) != len(selected_service_ids):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="service_ids must not contain duplicates")
        requested_master, booking_master = await self.resolve_booking_master(session, master_id)
        services = await self.resolve_booking_services_for_master(
            session,
            requested_master,
            booking_master,
            selected_service_ids,
        )
        if self.is_closed_business_day(target_date):
            return []

        day_start, day_end = self.day_bounds(target_date)
        availability_windows = await self.list_availability_windows(session, booking_master.id, day_start, day_end)
        if not availability_windows:
            return []

        bookings = await self.list_busy_bookings(session, booking_master.id, day_start, day_end)
        blocks = await self.list_time_blocks(session, booking_master.id, day_start, day_end)
        busy_intervals = [(item.start_at, item.end_at) for item in bookings] + [
            (item.start_at, item.end_at) for item in blocks
        ]

        slots: list[AvailableSlotResponse] = []
        now = datetime.now(KYIV_TZ)
        step = timedelta(minutes=SLOT_STEP_MINUTES)
        duration = timedelta(minutes=duration_minutes or sum(item.duration_minutes for item in services))
        for window in availability_windows:
            window_start = max(self.normalize_datetime(window.start_at), day_start)
            window_end = min(self.normalize_datetime(window.end_at), day_end)
            current = window_start
            while current + duration <= window_end:
                slot_end = current + duration
                if current > now and not any(
                    self.intervals_overlap(existing_start, existing_end, current, slot_end)
                    for existing_start, existing_end in busy_intervals
                ):
                    slots.append(AvailableSlotResponse(start_at=current, end_at=slot_end))
                current += step
        return slots

    async def ensure_slot_available(
        self,
        session: AsyncSession,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
        exclude_booking_id: int | None = None,
    ) -> None:
        bookings = await self.list_busy_bookings(session, master_id, start_at, end_at, exclude_booking_id)
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

    async def get_or_create_booking_customer(
        self,
        session: AsyncSession,
        payload: PublicBookingCreate,
    ) -> tuple[Customer, str]:
        normalized_phone = self.customer_auth_service.normalize_phone(payload.customer_phone)
        email = str(payload.customer_email).lower() if payload.customer_email else None

        stmt = select(Customer).where(Customer.phone == normalized_phone)
        customer = (await session.execute(stmt)).scalar_one_or_none()

        if customer is None and email is not None:
            customer = (
                await session.execute(select(Customer).where(Customer.email == email))
            ).scalar_one_or_none()

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
            services = await self.resolve_booking_services_for_master(
                session,
                requested_master,
                booking_master,
                payload.service_ids or [payload.service_id],
            )

            duration_minutes = payload.duration_minutes or sum(item.duration_minutes for item in services)
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
            )
            self.replace_booking_services(booking, services)
            await self.promotion_service.apply_to_booking(
                session,
                booking=booking,
                promotion_code=promotion_code,
                customer=customer,
                services=services,
                at=start_at,
                allow_private_promotions=allow_private_promotions,
            )
            session.add(booking)
            await session.flush()
            if payload.funnel_session_id is not None:
                self.booking_funnel_service.add_booking_success(
                    session,
                    booking_id=booking.id,
                    master_id=booking.master_id,
                    service_id=booking.service_id,
                    anonymous_session_id=payload.funnel_session_id,
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
