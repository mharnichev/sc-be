from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.booking import BarberService, Master
from app.schemas.booking_alternatives import (
    AlternativeMasterPublic,
    BookingAlternativeSlot,
    BookingAlternativesRequest,
    BookingAlternativesResponse,
)
from app.services.booking import KYIV_TZ, BookingServiceLayer


MAX_SAME_MASTER_SLOTS = 3
MAX_OTHER_MASTER_SLOTS = 6
# The configured booking horizon is itself bounded (currently two calendar months).
# Search that whole public horizon so "no alternatives" is authoritative.


class BookingAlternativesService:
    def __init__(self, booking_service: BookingServiceLayer | None = None) -> None:
        self.booking_service = booking_service or BookingServiceLayer()

    @staticmethod
    def _public_master(master: Master) -> AlternativeMasterPublic:
        return AlternativeMasterPublic(
            id=master.id,
            name=master.full_name_uk,
            photo_url=getattr(master, "photo_url", None),
            avatar_url=getattr(master, "avatar_url", None),
            role=getattr(master, "position_uk", None),
            # Reviews are not currently part of the public booking model.
            rating_summary=getattr(master, "rating_summary", None),
        )

    def _matching_service_ids(self, master: Master, selected: Sequence[BarberService]) -> list[int] | None:
        """Map catalog-equivalent services to this master without exposing private data."""
        matched: list[int] = []
        active = [item for item in master.services if self.booking_service.is_active_service(item)]
        for source in selected:
            service = next((item for item in active if item.id == source.id), None)
            if service is None and source.base_service_id is not None:
                service = next((item for item in active if item.base_service_id == source.base_service_id), None)
            if service is None and source.base_service_id is None:
                service = next(
                    (item for item in active if item.base_service_id is None and self.booking_service.custom_service_key(item) == self.booking_service.custom_service_key(source)),
                    None,
                )
            if service is None or service.id in matched:
                return None
            matched.append(service.id)
        return matched

    async def _slots_for_day(
        self, session: AsyncSession, master: Master, service_ids: list[int], day: date, duration: int
    ) -> list[BookingAlternativeSlot]:
        slots = await self.booking_service.get_available_slots(
            session, master.id, service_ids[0], day, service_ids=service_ids, duration_minutes=duration
        )
        if not slots:
            return []
        public_master = self._public_master(master)
        return [
            BookingAlternativeSlot(
                master=public_master, start_at=slot.start_at, end_at=slot.end_at,
                date=slot.start_at.astimezone(KYIV_TZ).date(),
                duration_minutes=duration,
            )
            for slot in slots
        ]

    async def find(self, session: AsyncSession, payload: BookingAlternativesRequest) -> BookingAlternativesResponse:
        if payload.desired_date < datetime.now(KYIV_TZ).date():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="desired_date must not be in the past")
        if payload.desired_date > self.booking_service.availability_horizon_end_date():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="desired_date is outside availability horizon")

        requested_master, booking_master = await self.booking_service.resolve_booking_master(session, payload.master_id)
        if not bool(getattr(requested_master, "show_on_master_block", True)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        source_services = await self.booking_service.get_active_services(session, payload.service_ids)
        self.booking_service.ensure_master_provides_services(requested_master, payload.service_ids)
        await self.booking_service.resolve_booking_services_for_master(
            session,
            requested_master,
            booking_master,
            payload.service_ids,
            source_services=source_services,
        )
        duration_minutes = self.booking_service.resolve_duration_minutes(
            source_services,
            payload.duration_minutes,
        )

        horizon = self.booking_service.availability_horizon_end_date()
        same_master: list[BookingAlternativeSlot] = []
        search_days = max(0, (horizon - payload.desired_date).days)
        # Same-master suggestions are dates after the failed requested date. The
        # desired day was already searched by the client before this endpoint.
        for offset in range(1, search_days + 1):
            day = payload.desired_date + timedelta(days=offset)
            if day > horizon:
                break
            if self.booking_service.is_closed_business_day(day):
                continue
            same_master.extend(
                await self._slots_for_day(
                    session,
                    requested_master,
                    [item.id for item in source_services],
                    day,
                    duration_minutes,
                )
            )
            if len(same_master) >= MAX_SAME_MASTER_SLOTS:
                break
        same_master = same_master[:MAX_SAME_MASTER_SLOTS]

        if not payload.another_master_acceptable:
            return BookingAlternativesResponse(same_master=same_master)

        masters = (
            await session.execute(
                select(Master)
                .options(selectinload(Master.services).selectinload(BarberService.base_service))
                .where(
                    Master.is_active.is_(True),
                    Master.show_on_master_block.is_(True),
                    Master.id != requested_master.id,
                )
                .order_by(Master.full_name.asc())
            )
        ).scalars().unique().all()
        candidates: list[tuple[Master, list[int]]] = []
        for master in masters:
            service_ids = self._matching_service_ids(master, source_services)
            if not service_ids:
                continue
            services_by_id = {item.id: item for item in master.services}
            candidate_duration = sum(services_by_id[item_id].duration_minutes for item_id in service_ids)
            if candidate_duration != duration_minutes:
                continue
            candidates.append((master, service_ids))

        other_masters: list[BookingAlternativeSlot] = []
        # Desired day first; only then search forward for the nearest usable day.
        for offset in range(search_days + 1):
            day = payload.desired_date + timedelta(days=offset)
            if day > horizon:
                break
            if self.booking_service.is_closed_business_day(day):
                continue
            day_slots: list[BookingAlternativeSlot] = []
            for master, service_ids in candidates:
                day_slots.extend(
                    await self._slots_for_day(
                        session,
                        master,
                        service_ids,
                        day,
                        duration_minutes,
                    )
                )
            if day_slots:
                other_masters = sorted(
                    day_slots,
                    key=lambda slot: (slot.start_at, slot.master.id),
                )[:MAX_OTHER_MASTER_SLOTS]
                break
        return BookingAlternativesResponse(same_master=same_master, other_masters=other_masters[:MAX_OTHER_MASTER_SLOTS])
