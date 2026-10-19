from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.booking as booking_module
from app.services.booking import (
    CALENDAR_MASTER_BATCH_SIZE,
    KYIV_TZ,
    BookingServiceLayer,
)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(booking_module, "datetime", FrozenDateTime)


class _CalendarReadProbe(BookingServiceLayer):
    def __init__(self) -> None:
        super().__init__()
        self.calendar_reads = 0
        self.master = SimpleNamespace(id=7, show_on_master_block=True, services=[SimpleNamespace(id=3)])
        self.source_service = SimpleNamespace(id=3, duration_minutes=30, price=100)

    async def resolve_booking_master(self, session, master_id, *, for_update=False):
        return self.master, self.master

    async def get_active_services(self, session, service_ids):
        return [self.source_service]

    async def resolve_booking_services_for_master(self, session, requested_master, booking_master, service_ids, *, source_services=None):
        return [self.source_service]

    async def list_availability_windows(self, *args, **kwargs):
        self.calendar_reads += 1
        return []

    async def list_busy_bookings(self, *args, **kwargs):
        self.calendar_reads += 1
        return []

    async def list_time_blocks(self, *args, **kwargs):
        self.calendar_reads += 1
        return []


def _at(day: date, minute: int) -> datetime:
    return datetime.combine(day, time(9, 0), tzinfo=KYIV_TZ) + timedelta(minutes=minute)


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _reference_slot_starts(
    day: date,
    duration: timedelta,
    windows: list[tuple[datetime, datetime]],
    busy: list[tuple[datetime, datetime]],
    now: datetime,
) -> list[datetime]:
    """Brute-force candidate oracle for the approved merged-window/off-grid policy."""
    day_start = datetime.combine(day, time(9, 30), tzinfo=KYIV_TZ)
    day_end = datetime.combine(day, time(20), tzinfo=KYIV_TZ)
    available = _merge([(max(start, day_start), min(end, day_end)) for start, end in windows])
    step = timedelta(minutes=15)
    candidates = {left for left, _ in available}
    candidates.update(end for _, end in busy)
    current = day_start
    while current + duration <= day_end:
        candidates.add(current)
        current += step
    return sorted(
        candidate for candidate in candidates
        if candidate > now
        and any(left <= candidate and candidate + duration <= right for left, right in available)
        and not any(busy_left < candidate + duration and busy_right > candidate for busy_left, busy_right in busy)
    )


@pytest.mark.anyio
async def test_monday_range_loads_calendar_before_returning_empty_unpublished_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monday = date(2026, 10, 5)
    _freeze_clock(monkeypatch, datetime(2026, 10, 1, 8, tzinfo=KYIV_TZ))
    service = _CalendarReadProbe()

    result = await service.get_available_slots_for_range(
        object(), 7, monday, monday, service_id=3,
    )

    assert service.calendar_reads == 3
    assert result.slots_by_date == {monday: []}


def test_slot_kernel_matches_seeded_independent_interval_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    day = date(2026, 10, 6)
    now = _at(day, 0)
    _freeze_clock(monkeypatch, now)
    randomizer = random.Random(917_263)
    service = BookingServiceLayer()

    for _ in range(96):
        def interval() -> tuple[datetime, datetime]:
            start = randomizer.randint(0, 690)
            return _at(day, start), _at(day, start + randomizer.randint(1, 180))

        windows = [interval() for _ in range(randomizer.randint(0, 6))]
        busy = [interval() for _ in range(randomizer.randint(0, 7))]
        duration = timedelta(minutes=randomizer.choice([15, 30, 45, 60, 75, 90]))
        expected = _reference_slot_starts(day, duration, windows, busy, now)
        actual = service._calendar_slot_starts(
            target_date=day,
            duration=duration,
            availability_intervals=windows,
            busy_intervals=busy,
        )
        data = service._calendar_range_data(
            calendar_master_ids=[7],
            start_date=day,
            end_date=day,
            availability_windows=[SimpleNamespace(start_at=start, end_at=end) for start, end in windows],
            bookings=[SimpleNamespace(start_at=start, end_at=end) for start, end in busy],
            blocks=[],
        )

        assert actual == expected
        assert service.calendar_slot_exists(
            data, calendar_master_id=7, target_date=day, duration=duration,
        ) is bool(expected)
        assert [start for _, start, _ in service.iter_calendar_slots(
            data, calendar_master_id=7, start_date=day, end_date=day, duration=duration,
        )] == expected


@pytest.mark.anyio
async def test_bulk_loader_uses_four_bounded_reads_and_groups_kyiv_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    day = date(2026, 10, 6)
    _freeze_clock(monkeypatch, _at(day, 0))

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class Session:
        def __init__(self):
            self.calls = []
            self.results = [
                [SimpleNamespace(master_id=7, start_at=_at(day, 30).astimezone(UTC), end_at=_at(day, 150).astimezone(UTC))],
                [SimpleNamespace(master_id=8, start_at=_at(day, 90), end_at=_at(day, 120))],
                [SimpleNamespace(master_id=7, start_at=_at(day, 180), end_at=_at(day, 210))],
                [SimpleNamespace(master_id=8, start_at=_at(day, 240), end_at=_at(day, 270))],
            ]

        async def execute(self, statement):
            self.calls.append(statement)
            return Result(self.results.pop(0))

    session = Session()
    service = BookingServiceLayer()
    start, _ = service.day_bounds(day)
    _, end = service.day_bounds(day)
    data = await service.load_calendar_range_data(session, [7, 8, 7], start, end)

    assert len(session.calls) == 4
    assert tuple(data.availability_by_master_id) == (7, 8)
    assert data.availability_by_master_id[7][day] == ((_at(day, 30), _at(day, 150)),)
    assert data.busy_by_master_id[7][day] == ((_at(day, 180), _at(day, 210)),)
    assert data.busy_by_master_id[8][day] == ((_at(day, 90), _at(day, 120)), (_at(day, 240), _at(day, 270)))


@pytest.mark.anyio
async def test_bulk_loader_batches_more_than_256_calendar_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    day = date(2026, 10, 6)
    _freeze_clock(monkeypatch, _at(day, 0))

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        def __init__(self):
            self.calls = 0

        async def execute(self, statement):
            self.calls += 1
            return Result()

    ids = list(range(1, CALENDAR_MASTER_BATCH_SIZE + 2))
    service = BookingServiceLayer()
    start, _ = service.day_bounds(day)
    _, end = service.day_bounds(day)
    session = Session()
    data = await service.load_calendar_range_data(session, ids, start, end)

    assert session.calls == 8
    assert tuple(data.availability_by_master_id) == tuple(ids)
    assert tuple(data.busy_by_master_id) == tuple(ids)


@pytest.mark.anyio
async def test_exists_only_range_agrees_with_full_slots_without_slot_dtos(monkeypatch: pytest.MonkeyPatch) -> None:
    day = date(2026, 10, 6)
    _freeze_clock(monkeypatch, _at(day, 0))

    class Probe(_CalendarReadProbe):
        async def list_availability_windows(self, *args, **kwargs):
            self.calendar_reads += 1
            return [SimpleNamespace(start_at=_at(day, 30), end_at=_at(day, 180))]

    service = Probe()
    full = await service.get_available_slots_for_range(object(), 7, day, day, service_id=3)
    exists = await service.get_available_slots_for_range(object(), 7, day, day, service_id=3, exists_only=True)

    assert exists.slots_by_date == {day: []}
    assert exists.has_slots_by_date == {day: bool(full.slots_by_date[day])}


@pytest.mark.anyio
async def test_invalid_duration_is_rejected_before_monday_calendar_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monday = date(2026, 10, 5)
    _freeze_clock(monkeypatch, datetime(2026, 10, 1, 8, tzinfo=KYIV_TZ))
    service = _CalendarReadProbe()

    with pytest.raises(HTTPException) as error:
        await service.get_available_slots_for_range(object(), 7, monday, monday, service_id=3, duration_minutes=45)

    assert getattr(error.value, "status_code", None) == 400
    assert service.calendar_reads == 0


@pytest.mark.anyio
async def test_inactive_catalog_is_rejected_before_monday_calendar_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monday = date(2026, 10, 5)
    _freeze_clock(monkeypatch, datetime(2026, 10, 1, 8, tzinfo=KYIV_TZ))

    class InactiveCatalogProbe(_CalendarReadProbe):
        async def get_active_services(self, session, service_ids):
            raise HTTPException(status_code=404, detail="Service not found")

    service = InactiveCatalogProbe()
    with pytest.raises(HTTPException) as error:
        await service.get_available_slots_for_range(object(), 7, monday, monday, service_id=3)

    assert error.value.status_code == 404
    assert service.calendar_reads == 0
