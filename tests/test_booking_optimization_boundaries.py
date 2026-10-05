from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.booking as booking_module
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.booking_alternatives import BookingAlternativesService
from tests.test_booking_alternatives_optimization import (
    MemoryBooking,
    MemorySession,
    memory_master,
)


def freeze_clock(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(booking_module, "datetime", FrozenDateTime)


class RangeProbe(BookingServiceLayer):
    """Real range/existence implementation with only persistence substituted."""

    def __init__(self, windows: list[object]) -> None:
        super().__init__()
        self.windows = windows
        self.master = SimpleNamespace(id=7, show_on_master_block=True, services=[SimpleNamespace(id=3)])
        self.catalog_service = SimpleNamespace(id=3, duration_minutes=30, price=Decimal(100))

    async def resolve_booking_master(self, session, master_id, *, for_update=False):
        return self.master, self.master

    async def get_active_services(self, session, service_ids):
        return [self.catalog_service]

    async def resolve_booking_services_for_master(self, session, requested_master, booking_master, service_ids, *, source_services=None):
        return [self.catalog_service]

    async def list_availability_windows(self, session, master_id, start_at, end_at):
        return [item for item in self.windows if item.start_at < end_at and item.end_at > start_at]

    async def list_busy_bookings(self, *args, **kwargs):
        return []

    async def list_time_blocks(self, *args, **kwargs):
        return []


class Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class LoaderSession:
    def __init__(self, result_sets):
        self.result_sets = list(result_sets)

    async def execute(self, statement):
        return Result(self.result_sets.pop(0))


@pytest.mark.anyio
async def test_loader_groups_utc_midnight_crossing_and_dst_day_into_kyiv_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch, datetime(2026, 3, 28, 8, tzinfo=KYIV_TZ))
    service = BookingServiceLayer()
    dst_day = date(2026, 3, 29)
    midnight_day = date(2026, 10, 6)
    dst_start = datetime(2026, 3, 29, 9, 30, tzinfo=KYIV_TZ)
    dst_end = datetime(2026, 3, 29, 11, 0, tzinfo=KYIV_TZ)
    # This UTC interval starts on the previous UTC date but reaches Kyiv work hours.
    midnight_start = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    midnight_end = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    session = LoaderSession([
        [
            SimpleNamespace(master_id=7, start_at=dst_start.astimezone(UTC), end_at=dst_end.astimezone(UTC)),
            SimpleNamespace(master_id=8, start_at=midnight_start, end_at=midnight_end),
        ],
        [], [], [],
    ])
    start = datetime(2026, 3, 29, 0, tzinfo=KYIV_TZ)
    end = datetime(2026, 10, 6, 20, tzinfo=KYIV_TZ)
    data = await service.load_calendar_range_data(session, [7, 8], start, end)

    assert service.calendar_slot_exists(
        data, calendar_master_id=7, target_date=dst_day, duration=timedelta(minutes=60),
    )
    assert next(start_at for _, start_at, _ in service.iter_calendar_slots(
        data, calendar_master_id=8, start_date=midnight_day, end_date=midnight_day,
        duration=timedelta(minutes=30),
    )) == datetime(2026, 10, 6, 9, 30, tzinfo=KYIV_TZ)


@pytest.mark.anyio
async def test_range_and_existence_accept_exact_today_and_horizon_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 1, 8, tzinfo=KYIV_TZ)
    freeze_clock(monkeypatch, now)
    today = now.date()
    horizon = date(2026, 12, 1)
    windows = [
        SimpleNamespace(start_at=datetime.combine(today, time(9, 30), tzinfo=KYIV_TZ),
                        end_at=datetime.combine(today, time(11), tzinfo=KYIV_TZ)),
        SimpleNamespace(start_at=datetime.combine(horizon, time(9, 30), tzinfo=KYIV_TZ),
                        end_at=datetime.combine(horizon, time(11), tzinfo=KYIV_TZ)),
    ]
    service = RangeProbe(windows)

    for boundary in (today, horizon):
        full = await service.get_available_slots_for_range(object(), 7, boundary, boundary, service_id=3)
        exists = await service.get_available_slots_range(object(), 7, boundary, boundary, service_id=3)
        assert full.slots_by_date[boundary]
        assert exists == [(boundary, True)]

    with pytest.raises(HTTPException) as before_today:
        await service.get_available_slots_range(object(), 7, today - timedelta(days=1), today - timedelta(days=1), service_id=3)
    with pytest.raises(HTTPException) as after_horizon:
        await service.get_available_slots_range(object(), 7, horizon + timedelta(days=1), horizon + timedelta(days=1), service_id=3)
    assert before_today.value.status_code == after_horizon.value.status_code == 400


@pytest.mark.anyio
async def test_optimized_alternatives_exclude_inactive_base_candidate_before_bulk_load() -> None:
    requested = memory_master(1)
    inactive_candidate = memory_master(2, redirect=9)
    inactive_candidate.services[0].base_service.is_active = False
    calendar = memory_master(9, public=False)
    booking = MemoryBooking([requested, inactive_candidate, calendar])
    booking.open(1, booking.target + timedelta(days=1))

    result = await BookingAlternativesService(booking).find_page(
        MemorySession(booking), booking.request(1, another_master_acceptable=True),
    )

    assert result.other_masters == []
    assert booking.bulk_calls == [{1}]


@pytest.mark.anyio
async def test_optimized_custom_service_redirect_mapping_keeps_public_identity() -> None:
    requested = memory_master(1, 30, 500, redirect=9, base_id=None)
    candidate = memory_master(2, 30, 500, redirect=9, base_id=None)
    calendar = memory_master(9, 30, 500, public=False, base_id=None)
    for master, title in ((requested, "Custom fade"), (candidate, " custom FADE "), (calendar, "Custom fade")):
        service = master.services[0]
        service.name = title
        service.title_uk = title
        service.base_service = None
    booking = MemoryBooking([requested, candidate, calendar])
    booking.open(9, booking.target)
    booking.open(9, booking.target + timedelta(days=1))

    result = await BookingAlternativesService(booking).find_page(
        MemorySession(booking), booking.request(1, another_master_acceptable=True, page_size=100),
    )

    assert result.same_master and all(item.master.id == 1 and item.service_ids == [10] for item in result.same_master)
    assert result.other_masters and all(item.master.id == 2 and item.service_ids == [20] for item in result.other_masters)
    assert booking.bulk_calls == [{9}]


@pytest.mark.anyio
async def test_service_id_permutation_preserves_offer_identity_and_caller_service_order() -> None:
    master = memory_master(1, 30, 100, base_id=100)
    master.services.append(SimpleNamespace(
        id=11, master_id=1, name="Beard", title_uk="Beard", title_en=None,
        duration_minutes=45, price=Decimal(200), is_active=True,
        base_service_id=101, base_service=SimpleNamespace(is_active=True),
    ))
    booking = MemoryBooking([master])
    booking.open(1, booking.target + timedelta(days=1))
    service = BookingAlternativesService(booking)
    first = await service.find_page(MemorySession(booking), BookingAlternativesRequest(
        master_id=1, service_ids=[10, 11], desired_date=booking.target, duration_minutes=75, page_size=100,
    ))
    second = await service.find_page(MemorySession(booking), BookingAlternativesRequest(
        master_id=1, service_ids=[11, 10], desired_date=booking.target, duration_minutes=75, page_size=100,
    ))

    assert first.search_context_id == second.search_context_id
    assert [item.offer_id for item in first.same_master] == [item.offer_id for item in second.same_master]
    assert [(item.start_at, item.end_at, item.price, item.duration_minutes) for item in first.same_master] == [
        (item.start_at, item.end_at, item.price, item.duration_minutes) for item in second.same_master
    ]
    # The response keeps the caller's service sequence, while the opaque search
    # and offer identities remain canonical over the selected service set.
    assert all(item.service_ids == [10, 11] for item in first.same_master)
    assert all(item.service_ids == [11, 10] for item in second.same_master)
