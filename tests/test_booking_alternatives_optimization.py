from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.booking_alternatives as alternatives_module
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.booking_alternatives import BookingAlternativesService


def memory_master(master_id, duration=30, price=500, *, redirect=None, public=True, base_id=1):
    service = SimpleNamespace(id=master_id * 10, master_id=master_id, name='Haircut', title_uk='Haircut',
                              title_en='Haircut', duration_minutes=duration, price=Decimal(str(price)),
                              is_active=True, base_service_id=base_id,
                              base_service=SimpleNamespace(is_active=True) if base_id else None)
    return SimpleNamespace(id=master_id, full_name=f'Master {master_id}', full_name_uk=f'Master {master_id}',
                           services=[service], is_active=True, show_on_master_block=public,
                           booking_redirect_master_id=redirect)


class MemoryBooking(BookingServiceLayer):
    """Replace persistence only; use the production mapping and interval rules."""
    def __init__(self, masters, *, days=8):
        super().__init__()
        self.masters = {item.id: item for item in masters}
        self.target = datetime.now(KYIV_TZ).date() + timedelta(days=2)
        while self.target.weekday() == 0:
            self.target += timedelta(days=1)
        self.horizon = self.target + timedelta(days=days)
        self.windows = []
        self.busy = []
        self.bulk_calls = []
        self.master_reads = []

    def availability_horizon_end_date(self):
        return self.horizon

    async def get_active_master_with_services(self, session, master_id, **kwargs):
        self.master_reads.append(master_id)
        master = self.masters.get(master_id)
        if not master or not master.is_active:
            raise HTTPException(404, 'Master not found')
        return master

    async def get_active_service(self, session, service_id):
        service = next((s for m in self.masters.values() for s in m.services if s.id == service_id), None)
        if not service or not self.is_active_service(service):
            raise HTTPException(404, 'Service not found')
        return service

    async def list_availability_windows(self, session, master_id, start_at, end_at):
        return [w for w in self.windows if w.master_id == master_id and w.start_at < end_at and w.end_at > start_at]

    async def list_busy_bookings(self, session, master_id, start_at, end_at, *args):
        return [w for w in self.busy if w.master_id == master_id and w.start_at < end_at and w.end_at > start_at]

    async def list_time_blocks(self, *args):
        return []

    async def load_calendar_range_data(self, session, *, calendar_master_ids, start_at, end_at):
        self.bulk_calls.append(set(calendar_master_ids))
        return self._calendar_range_data(calendar_master_ids=calendar_master_ids,
                                         start_date=start_at.date(), end_date=end_at.date(),
                                         availability_windows=self.windows, bookings=self.busy, blocks=[])

    def open(self, master_id, day=None, *, minutes=630, start_minute=570):
        day = day or self.target
        start = datetime.combine(day, datetime.min.time(), tzinfo=KYIV_TZ) + timedelta(minutes=start_minute)
        self.windows.append(SimpleNamespace(master_id=master_id, start_at=start, end_at=start+timedelta(minutes=minutes)))
        return start

    def block(self, master_id, start, end):
        self.busy.append(SimpleNamespace(master_id=master_id, start_at=start, end_at=end))

    def request(self, master_id=1, **overrides):
        services = self.masters[master_id].services
        values = {"master_id": master_id, "service_ids": [s.id for s in services], "desired_date": self.target,
                  "duration_minutes": sum(s.duration_minutes for s in services)}
        return BookingAlternativesRequest(**(values | overrides))


class MemorySession:
    def __init__(self, booking, selected=1):
        self.booking = booking
        self.selected = selected

    async def execute(self, statement):
        masters = sorted((m for m in self.booking.masters.values()
                          if m.is_active and m.show_on_master_block and m.id != self.selected), key=lambda m:m.full_name)
        return SimpleNamespace(scalars=lambda:SimpleNamespace(unique=lambda:SimpleNamespace(all=lambda:masters)))


@pytest.mark.anyio
async def test_page_materializes_only_requested_public_models(monkeypatch):
    booking = MemoryBooking([memory_master(i) for i in range(1, 5)])
    for master in booking.masters.values():
        for offset in range(9):
            booking.open(master.id, booking.target + timedelta(days=offset))
    service = BookingAlternativesService(booking)
    real_model = alternatives_module.BookingAlternativeSlot
    created = []
    def count_model(**kwargs):
        created.append(kwargs['offer_id'])
        return real_model(**kwargs)
    monkeypatch.setattr(alternatives_module, 'BookingAlternativeSlot', count_model)
    page = await service.find_page(MemorySession(booking), booking.request())
    assert page.total_count > 500
    assert len(created) == len(page.same_master) + len(page.other_masters) == 9
    assert booking.bulk_calls == [{1, 2, 3, 4}]
    assert booking.master_reads == [1]  # candidate catalogs already loaded by the public query


@pytest.mark.anyio
async def test_every_page_equals_complete_response_and_has_no_missing_offers():
    booking = MemoryBooking([memory_master(1), memory_master(2, 45, 750), memory_master(3)])
    for master in booking.masters.values():
        for offset in range(3):
            booking.open(master.id, booking.target + timedelta(days=offset))
    service = BookingAlternativesService(booking)
    session = MemorySession(booking)
    request = booking.request(page_size=17)
    full = await service.find(session, request)
    seen=[]
    offset=0
    while True:
        payload=request.model_copy(update={'offset':offset})
        page=await service.find_page(session, payload)
        expected=service.paginate(full,offset=offset,page_size=17)
        assert page.model_dump() == expected.model_dump()
        seen.extend(s.offer_id for s in [*page.same_master,*page.other_masters])
        if page.next_offset is None:break
        offset=page.next_offset
    assert len(seen)==len(set(seen))==len(full.same_master)+len(full.other_masters)
    empty=await service.find_page(session,request.model_copy(update={'offset':len(seen)+10}))
    assert empty.same_master==empty.other_masters==[]
    assert empty.next_offset is None and empty.total_count==len(seen)


@pytest.mark.anyio
async def test_shared_redirect_calendar_loaded_once_with_source_duration_and_mapped_price():
    masters=[memory_master(1,60,500,redirect=9),memory_master(2,45,600,redirect=9),
             memory_master(3,45,700,redirect=9),memory_master(9,30,800,public=False)]
    booking=MemoryBooking(masters)
    booking.open(9)
    booking.open(9,booking.target+timedelta(days=1))
    result=await BookingAlternativesService(booking).find_page(MemorySession(booking),booking.request(page_size=100))
    all_slots=[*result.same_master,*result.other_masters]
    assert all_slots and all(s.price==800 for s in all_slots)
    assert all(s.master.id==1 and s.duration_minutes==60 and s.service_ids==[10] for s in result.same_master)
    assert all(s.master.id==2 and s.duration_minutes==45 and s.service_ids==[20] for s in result.other_masters)
    assert len({s.start_at for s in all_slots})==len(all_slots)
    assert booking.bulk_calls==[{9}]
    assert booking.master_reads==[1,9]


@pytest.mark.anyio
async def test_page_refreshes_busy_state_and_does_not_cache_offer_availability():
    booking=MemoryBooking([memory_master(1),memory_master(2)])
    start=booking.open(2)
    service=BookingAlternativesService(booking)
    session=MemorySession(booking)
    before=await service.find_page(session,booking.request())
    booking.block(2,start,start+timedelta(minutes=30))
    after=await service.find_page(session,booking.request())
    assert before.total_count>after.total_count
    assert all(s.start_at>=start+timedelta(minutes=30) for s in after.other_masters)
    assert before.search_context_id==after.search_context_id


@pytest.mark.anyio
@pytest.mark.parametrize('invalid',['hidden','inactive','duration','service','horizon'])
async def test_invalid_requested_context_still_fails_before_calendar_reads(invalid):
    booking=MemoryBooking([memory_master(1)])
    payload=booking.request()
    if invalid=='hidden':booking.masters[1].show_on_master_block=False
    elif invalid=='inactive':booking.masters[1].is_active=False
    elif invalid=='duration':payload=payload.model_copy(update={'duration_minutes':31})
    elif invalid=='service':booking.masters[1].services[0].is_active=False
    elif invalid=='horizon':payload=payload.model_copy(update={'desired_date':booking.horizon+timedelta(days=1)})
    with pytest.raises(HTTPException) as error:
        await BookingAlternativesService(booking).find_page(MemorySession(booking),payload)
    assert error.value.status_code in (400,404)
    assert booking.bulk_calls==[]
