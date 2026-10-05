from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from fastapi import HTTPException
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

MAX_SAME_MASTER_RECOMMENDATIONS = 3
MAX_OTHER_MASTER_RECOMMENDATIONS = 6
# The configured booking horizon is itself bounded (currently two calendar months).
# Search that whole public horizon so "no alternatives" is authoritative.


@dataclass(frozen=True, slots=True)
class _OfferContext:
    master: AlternativeMasterPublic
    service_ids: tuple[int, ...]
    duration_minutes: int
    price: Decimal
    calendar_id: int
    start_date: date
    same_master: bool


@dataclass(frozen=True, slots=True)
class _Offer:
    context: _OfferContext
    start_at: datetime
    end_at: datetime
    date: date

    @property
    def master(self) -> AlternativeMasterPublic:
        return self.context.master

    @property
    def service_ids(self) -> tuple[int, ...]:
        return self.context.service_ids


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

    @staticmethod
    def _opaque_id(*parts: object) -> str:
        material = "|".join(str(item) for item in parts)
        return sha256(material.encode("utf-8")).hexdigest()[:32]

    def _search_context_id(self, payload: BookingAlternativesRequest, duration_minutes: int) -> str:
        return self._opaque_id(
            "booking-alternatives-v1",
            payload.master_id,
            ",".join(str(item) for item in sorted(payload.service_ids)),
            payload.desired_date.isoformat(),
            duration_minutes,
        )

    @staticmethod
    def _daypart(slot: BookingAlternativeSlot) -> str:
        hour = slot.start_at.astimezone(KYIV_TZ).hour
        if hour < 12:
            return "morning"
        if hour < 17:
            return "afternoon"
        return "evening"

    def _rank(
        self,
        slots: Sequence[BookingAlternativeSlot] | Sequence[_Offer],
        *,
        limit: int,
        diversify_masters: bool,
        desired_date: date | None = None,
    ) -> tuple[list, set[int]]:
        """Select the same diverse prefix without copying public response models."""
        ordered = sorted(slots, key=lambda item: (item.start_at, item.master.id, tuple(item.service_ids)))
        selected = []
        selected_ids: set[int] = set()
        seen_masters: set[int] = set()
        seen_dayparts: set[str] = set()
        seen_dates: set[date] = set()
        # Ranking needs the complete eligible set for exact global diversity;
        # public DTOs and opaque offer IDs are only constructed for the page.
        for _ in range(min(limit, len(ordered))):
            def score(item):
                diversity = (int(diversify_masters and item.master.id not in seen_masters)
                             + int(self._daypart(item) not in seen_dayparts)
                             + int(item.date not in seen_dates))
                return (int(diversify_masters and desired_date is not None and item.date != desired_date),
                        -diversity, item.start_at, item.master.id, tuple(item.service_ids))
            chosen = min((item for item in ordered if id(item) not in selected_ids), key=score)
            selected.append(chosen)
            selected_ids.add(id(chosen))
            seen_masters.add(chosen.master.id)
            seen_dayparts.add(self._daypart(chosen))
            seen_dates.add(chosen.date)
        return [*selected, *[item for item in ordered if id(item) not in selected_ids]], selected_ids

    def _recommend(self, slots, *, limit, diversify_masters, desired_date=None):
        ordered, recommended = self._rank(slots, limit=limit, diversify_masters=diversify_masters,
                                         desired_date=desired_date)
        return [item.model_copy(update={"recommended": True}) if id(item) in recommended else item
                for item in ordered]

    @staticmethod
    def paginate(
        response: BookingAlternativesResponse,
        *,
        offset: int,
        page_size: int,
    ) -> BookingAlternativesResponse:
        """Page complete results without changing the deterministic recommendation order."""
        recommended_same = [item for item in response.same_master if item.recommended]
        recommended_other = [item for item in response.other_masters if item.recommended]
        remaining = sorted(
            [
                *[item for item in response.same_master if not item.recommended],
                *[item for item in response.other_masters if not item.recommended],
            ],
            key=lambda item: (item.date, item.start_at, item.master.id, tuple(item.service_ids)),
        )
        ordered = [*recommended_same, *recommended_other, *remaining]
        page = ordered[offset:offset + page_size]
        same_offer_ids = {item.offer_id for item in response.same_master}
        next_offset = offset + len(page)
        return response.model_copy(update={
            "same_master": [item for item in page if item.offer_id in same_offer_ids],
            "other_masters": [item for item in page if item.offer_id not in same_offer_ids],
            "total_count": len(ordered),
            "next_offset": next_offset if next_offset < len(ordered) else None,
        })

    async def _candidate_calendar(self, session, master: Master, masters: dict[int, Master]) -> Master:
        """Resolve redirects from the request's loaded catalog, fetching hidden targets once."""
        current = master
        visited = {current.id}
        while target_id := getattr(current, "booking_redirect_master_id", None):
            if target_id in visited:
                raise HTTPException(status_code=400, detail="Booking redirect cycle detected")
            visited.add(target_id)
            if target_id not in masters:
                masters[target_id] = await self.booking_service.get_active_master_with_services(session, target_id)
            current = masters[target_id]
        return current

    async def _contexts(self, session: AsyncSession, payload: BookingAlternativesRequest):
        service = self.booking_service
        if payload.desired_date < datetime.now(KYIV_TZ).date():
            raise HTTPException(status_code=400, detail="desired_date must not be in the past")
        horizon = service.availability_horizon_end_date()
        if payload.desired_date > horizon:
            raise HTTPException(status_code=400, detail="desired_date is outside availability horizon")
        requested, calendar = await service.resolve_booking_master(session, payload.master_id)
        if not bool(getattr(requested, "show_on_master_block", True)):
            raise HTTPException(status_code=404, detail="Master not found")
        source_services = await service.get_active_services(session, payload.service_ids)
        service.ensure_master_provides_services(requested, payload.service_ids)
        resolved = await service.resolve_booking_services_for_master(
            session, requested, calendar, payload.service_ids, source_services=source_services)
        duration = service.resolve_duration_minutes(source_services, payload.duration_minutes)
        search_id = self._search_context_id(payload, duration)

        def context(master, calendar_master, services, mapped, first_day, same):
            return _OfferContext(
                self._public_master(master), tuple(item.id for item in services),
                sum(item.duration_minutes for item in services),
                sum((Decimal(str(getattr(item, "price", 0) or 0)) for item in mapped), Decimal(0)),
                calendar_master.id, first_day, same,
            )
        contexts = []
        if payload.desired_date < horizon:
            contexts.append(context(requested, calendar, source_services, resolved,
                                    payload.desired_date + timedelta(days=1), True))
        if payload.another_master_acceptable:
            candidates = (await session.execute(
                select(Master).options(selectinload(Master.services).selectinload(BarberService.base_service))
                .where(Master.is_active.is_(True), Master.show_on_master_block.is_(True),
                       Master.id != requested.id).order_by(Master.full_name.asc())
            )).scalars().unique().all()
            masters = {item.id: item for item in [*candidates, requested, calendar]}
            for master in candidates:
                ids = self._matching_service_ids(master, source_services)
                if ids is None:
                    continue
                by_id = {item.id: item for item in master.services}
                sources = [by_id[item_id] for item_id in ids]
                try:
                    target = await self._candidate_calendar(session, master, masters)
                    mapped = await service.resolve_booking_services_for_master(
                        session, master, target, ids, source_services=sources)
                    contexts.append(context(master, target, sources, mapped, payload.desired_date, False))
                except HTTPException as exc:
                    if exc.status_code not in {400, 404}:
                        raise
        return search_id, horizon, contexts

    async def _find(self, session: AsyncSession, payload: BookingAlternativesRequest, *, paged: bool):
        search_id, horizon, contexts = await self._contexts(session, payload)
        if not contexts:
            return BookingAlternativesResponse(search_context_id=search_id)
        service = self.booking_service
        first_date = min(item.start_date for item in contexts)
        range_start, _ = service.day_bounds(first_date)
        _, range_end = service.day_bounds(horizon)
        data = await service.load_calendar_range_data(
            session, calendar_master_ids=[item.calendar_id for item in contexts],
            start_at=range_start, end_at=range_end,
        )
        same: list[_Offer] = []
        others: list[_Offer] = []
        physical_slots: set[tuple[int, datetime]] = set()
        for context in contexts:
            for day, start, end in service.iter_calendar_slots(
                data, calendar_master_id=context.calendar_id, start_date=context.start_date,
                end_date=horizon, duration=timedelta(minutes=context.duration_minutes),
            ):
                identity = (context.calendar_id, start)
                if identity in physical_slots:
                    continue
                physical_slots.add(identity)
                (same if context.same_master else others).append(_Offer(context, start, end, day))
        same, rec_same = self._rank(same, limit=MAX_SAME_MASTER_RECOMMENDATIONS, diversify_masters=False)
        others, rec_other = self._rank(others, limit=MAX_OTHER_MASTER_RECOMMENDATIONS,
                                       diversify_masters=True, desired_date=payload.desired_date)
        recommended = rec_same | rec_other
        total = len(same) + len(others)
        if paged:
            remainder = sorted((item for item in [*same, *others] if id(item) not in recommended),
                               key=lambda item: (item.date, item.start_at, item.master.id, item.service_ids))
            ordered = [*[item for item in same if id(item) in rec_same],
                       *[item for item in others if id(item) in rec_other], *remainder]
            chosen = ordered[payload.offset:payload.offset + payload.page_size]
        else:
            chosen = [*same, *others]
        result_same, result_other = [], []
        for item in chosen:
            context = item.context
            dto = BookingAlternativeSlot(
                master=context.master, start_at=item.start_at, end_at=item.end_at, date=item.date,
                duration_minutes=context.duration_minutes, service_ids=list(context.service_ids),
                price=context.price, search_context_id=search_id, recommended=id(item) in recommended,
                offer_id=self._opaque_id("booking-alternative-offer-v1", search_id, context.calendar_id,
                                         item.start_at.astimezone(KYIV_TZ).isoformat(), context.duration_minutes),
            )
            (result_same if context.same_master else result_other).append(dto)
        next_offset = payload.offset + len(chosen)
        return BookingAlternativesResponse(
            search_context_id=search_id, same_master=result_same, other_masters=result_other,
            total_count=total if paged else 0,
            next_offset=next_offset if paged and next_offset < total else None,
        )

    async def find(self, session: AsyncSession, payload: BookingAlternativesRequest) -> BookingAlternativesResponse:
        """Full-result compatibility entry point; HTTP requests use find_page."""
        return await self._find(session, payload, paged=False)

    async def find_page(self, session: AsyncSession, payload: BookingAlternativesRequest) -> BookingAlternativesResponse:
        """Exact live search with only page-sized public response materialization."""
        return await self._find(session, payload, paged=True)
