from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.booking import BarberService, BaseService, Booking, BookingServiceItem, BookingStatus, Master, MasterTimeBlock
from app.models.customer import Customer
from app.schemas.booking import AvailableSlotResponse, MasterTimeBlockCreate, PublicBookingCreate
from app.services.customer_auth import CustomerAuthService

KYIV_TZ = ZoneInfo("Europe/Kyiv")
WORK_START = time(hour=8)
WORK_END = time(hour=20)
SLOT_STEP_MINUTES = 15
ACTIVE_BOOKING_STATUSES = (BookingStatus.confirmed,)
MONDAY = 0
CLOSED_WEEKDAYS = {MONDAY}


class BookingServiceLayer:
    def __init__(self) -> None:
        self.customer_auth_service = CustomerAuthService()

    def normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=KYIV_TZ)
        return value.astimezone(KYIV_TZ)

    def day_bounds(self, target_date: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(target_date, WORK_START, tzinfo=KYIV_TZ),
            datetime.combine(target_date, WORK_END, tzinfo=KYIV_TZ),
        )

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

    def ensure_not_past(self, start_at: datetime) -> None:
        if self.normalize_datetime(start_at) <= datetime.now(KYIV_TZ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Slot is in the past")

    def intervals_overlap(self, existing_start: datetime, existing_end: datetime, start_at: datetime, end_at: datetime) -> bool:
        return existing_start < end_at and existing_end > start_at

    async def get_active_master_with_services(self, session: AsyncSession, master_id: int) -> Master:
        stmt = (
            select(Master)
            .options(selectinload(Master.services))
            .where(Master.id == master_id, Master.is_active.is_(True))
        )
        master = (await session.execute(stmt)).scalar_one_or_none()
        if not master:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        return master

    async def get_active_service(self, session: AsyncSession, service_id: int) -> BarberService:
        service = await session.get(BarberService, service_id)
        if not service or not service.is_active:
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
        master = await self.get_active_master_with_services(session, master_id)
        services = await self.get_active_services(session, selected_service_ids)
        self.ensure_master_provides_services(master, [item.id for item in services])
        if self.is_closed_business_day(target_date):
            return []

        day_start, day_end = self.day_bounds(target_date)
        bookings = await self.list_busy_bookings(session, master.id, day_start, day_end)
        blocks = await self.list_time_blocks(session, master.id, day_start, day_end)
        busy_intervals = [(item.start_at, item.end_at) for item in bookings] + [
            (item.start_at, item.end_at) for item in blocks
        ]

        slots: list[AvailableSlotResponse] = []
        now = datetime.now(KYIV_TZ)
        step = timedelta(minutes=SLOT_STEP_MINUTES)
        duration = timedelta(minutes=duration_minutes or sum(item.duration_minutes for item in services))
        current = day_start
        while current + duration <= day_end:
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

    async def create_public_booking(self, session: AsyncSession, payload: PublicBookingCreate) -> Booking:
        start_at = self.normalize_datetime(payload.start_at)
        self.ensure_not_past(start_at)

        async with session.begin():
            master_stmt = (
                select(Master)
                .options(selectinload(Master.services))
                .where(Master.id == payload.master_id, Master.is_active.is_(True))
                .with_for_update()
            )
            master = (await session.execute(master_stmt)).scalar_one_or_none()
            if not master:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
            services = await self.get_active_services(session, payload.service_ids or [payload.service_id])
            self.ensure_master_provides_services(master, [item.id for item in services])

            duration_minutes = payload.duration_minutes or sum(item.duration_minutes for item in services)
            end_at = start_at + timedelta(minutes=duration_minutes)
            self.ensure_within_working_hours(start_at, end_at)
            await self.ensure_slot_available(session, master.id, start_at, end_at)
            customer, customer_phone = await self.get_or_create_booking_customer(session, payload)

            booking = Booking(
                master_id=master.id,
                service_id=services[0].id,
                customer_id=customer.id,
                customer_name=payload.customer_name,
                customer_phone=customer_phone,
                customer_email=customer.email,
                customer_comment=payload.customer_comment,
                start_at=start_at,
                end_at=end_at,
                status=BookingStatus.confirmed,
            )
            self.replace_booking_services(booking, services)
            session.add(booking)

        await session.refresh(booking)
        return booking

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
