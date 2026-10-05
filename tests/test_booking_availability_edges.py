from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

import app.services.booking as booking_module
from app.schemas.booking import AvailableSlotResponse
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.booking_alternatives import BookingAlternativesService


TARGET_DAY = date(2026, 10, 6)  # Tuesday


def freeze_clock(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(booking_module, "datetime", FrozenDateTime)


def interval(hour: int, minute: int = 0, *, day: date = TARGET_DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=KYIV_TZ)


def slots(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    *,
    windows: list[tuple[datetime, datetime]] | None = None,
    busy: list[tuple[datetime, datetime]] | None = None,
    duration_minutes: int = 30,
    day: date = TARGET_DAY,
) -> list[AvailableSlotResponse]:
    freeze_clock(monkeypatch, now)
    service = BookingServiceLayer()
    return service._slots_from_calendar(
        target_date=day,
        duration=timedelta(minutes=duration_minutes),
        availability_windows=[
            SimpleNamespace(start_at=start, end_at=end)
            for start, end in (windows or [(interval(9, 30, day=day), interval(20, day=day))])
        ],
        busy_intervals=busy or [],
    )


@pytest.mark.parametrize(
    ("now", "first_start"),
    [
        (interval(8), interval(9, 30)),
        (interval(10), interval(10, 15)),
        (interval(20), None),
    ],
)
def test_slots_respect_before_opening_during_day_and_after_close(
    monkeypatch: pytest.MonkeyPatch, now: datetime, first_start: datetime | None,
) -> None:
    result = slots(monkeypatch, now)

    assert (result[0].start_at if result else None) == first_start


def test_touching_busy_interval_is_free_but_overlap_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    result = slots(monkeypatch, interval(8), busy=[(interval(10), interval(11))])
    starts = {item.start_at for item in result}

    assert interval(9, 30) in starts  # ends exactly when busy time begins
    assert interval(10) not in starts
    assert interval(11) in starts  # starts exactly when busy time ends


def test_exact_closing_bound_is_included(monkeypatch: pytest.MonkeyPatch) -> None:
    result = slots(monkeypatch, interval(8))
    starts = {item.start_at for item in result}

    assert interval(19, 30) in starts
    assert interval(19, 45) not in starts


def test_adjacent_published_windows_are_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    result = slots(
        monkeypatch,
        interval(8),
        windows=[(interval(9, 30), interval(10)), (interval(10), interval(10, 30))],
        duration_minutes=60,
    )

    assert [item.start_at for item in result] == [interval(9, 30)]


def test_off_grid_block_end_adds_free_start_and_keeps_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    result = slots(monkeypatch, interval(8), busy=[(interval(10), interval(10, 7))])
    starts = {item.start_at for item in result}

    assert interval(9, 30) in starts
    assert interval(9, 45) not in starts
    assert interval(10) not in starts
    assert interval(10, 7) in starts
    assert interval(10, 15) in starts
    assert interval(10, 22) not in starts


def test_monday_is_closed_even_with_a_published_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monday = date(2026, 10, 5)
    result = slots(monkeypatch, interval(8, day=monday), day=monday)

    assert result == []


def test_month_year_leap_and_dst_calendar_math(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch, datetime(2026, 12, 31, 12, tzinfo=KYIV_TZ))
    service = BookingServiceLayer()

    assert service.add_calendar_months(date(2026, 12, 31), 2) == date(2027, 2, 28)
    assert service.add_calendar_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert service.availability_horizon_end_date() == date(2027, 2, 28)
    dst_start = datetime(2026, 3, 29, 9, 30, tzinfo=KYIV_TZ)
    assert service.normalize_datetime(dst_start.astimezone(UTC)) == dst_start


def test_utc_instant_is_grouped_by_kyiv_calendar_date(monkeypatch: pytest.MonkeyPatch) -> None:
    instant_utc = datetime(2026, 10, 5, 21, 15, tzinfo=UTC)
    local_start = datetime(2026, 10, 6, 0, 15, tzinfo=KYIV_TZ)

    assert BookingServiceLayer().normalize_datetime(instant_utc) == local_start
    assert instant_utc.date() == date(2026, 10, 5)
    assert local_start.date() == TARGET_DAY


def test_equivalent_base_and_custom_services_require_active_compatible_catalog_entries() -> None:
    alternatives = BookingAlternativesService()
    base_source = SimpleNamespace(id=1, base_service_id=10, is_active=True, duration_minutes=30, price=500)
    custom_source = SimpleNamespace(
        id=2, base_service_id=None, is_active=True, name="Fade", title_uk="Fade", title_en=None,
        duration_minutes=30, price=500,
    )
    candidate = SimpleNamespace(services=[
        SimpleNamespace(id=11, base_service_id=10, is_active=True, duration_minutes=45, price=550),
        SimpleNamespace(id=12, base_service_id=None, is_active=True, name=" fade ", title_uk=" fade ", title_en=None,
                        duration_minutes=30, price=500),
        SimpleNamespace(id=13, base_service_id=10, is_active=False, duration_minutes=30, price=500),
    ])

    assert alternatives._matching_service_ids(candidate, [base_source, custom_source]) == [11, 12]
    assert alternatives._matching_service_ids(SimpleNamespace(services=[candidate.services[2]]), [base_source]) is None


@pytest.mark.parametrize("windows, expected", [
    ([(interval(10), interval(11)), (interval(11), interval(12))], True),
    ([(interval(10), interval(11, 15)), (interval(11), interval(12))], True),
    ([(interval(10), interval(11)), (interval(11, 1), interval(12))], False),
])
def test_hour_across_window_boundary(monkeypatch, windows, expected):
    result = slots(monkeypatch, interval(8), windows=windows, duration_minutes=60)
    starts = [item.start_at for item in result]
    assert (interval(10, 30) in starts) is expected
    assert len(starts) == len(set(starts))


@pytest.mark.parametrize("free_end, expected", [(interval(10, 37), True), (interval(10, 36), False)])
def test_merged_busy_intervals_and_exact_remaining_duration(monkeypatch, free_end, expected):
    result = slots(monkeypatch, interval(8), busy=[
        (interval(9, 30), interval(10, 3)),
        (interval(10), interval(10, 7)),
        (interval(9, 45), interval(10, 5)),
        (free_end, interval(20)),
    ])
    starts = [item.start_at for item in result]
    assert (interval(10, 7) in starts) is expected
    assert interval(10, 3) not in starts
    assert interval(10, 5) not in starts


def test_window_free_start_does_not_shift_quarter_hour_grid(monkeypatch):
    result = slots(monkeypatch, interval(8), windows=[(interval(10, 7), interval(11))])
    assert [item.start_at for item in result] == [interval(10, 7), interval(10, 15), interval(10, 30)]


@pytest.mark.anyio
async def test_final_validation_accepts_union_but_not_real_gap(monkeypatch):
    from fastapi import HTTPException
    from tests.test_booking import FakeSession
    freeze_clock(monkeypatch, interval(8))
    windows = [SimpleNamespace(start_at=interval(10), end_at=interval(11)),
               SimpleNamespace(start_at=interval(11), end_at=interval(12))]
    service = BookingServiceLayer()

    async def published_windows(*_args):
        return windows

    service.list_availability_windows = published_windows
    await service.ensure_booking_within_availability(
        FakeSession(execute_values=[None]), 1, interval(10, 30), interval(11, 30))
    windows[1].start_at = interval(11, 1)
    with pytest.raises(HTTPException) as error:
        await service.ensure_booking_within_availability(
            FakeSession(execute_values=[None]), 1, interval(10, 30), interval(11, 30))
    assert error.value.status_code == 409


def test_utc_windows_are_clipped_to_one_kyiv_working_day(monkeypatch):
    next_day = TARGET_DAY + timedelta(days=1)
    result = slots(monkeypatch, interval(8), windows=[
        (interval(19).astimezone(UTC), interval(20).astimezone(UTC)),
        (interval(9, 30, day=next_day).astimezone(UTC), interval(11, day=next_day).astimezone(UTC)),
    ], duration_minutes=90)
    assert result == []


def test_off_grid_free_start_in_past_does_not_create_a_new_now_start(monkeypatch):
    result = slots(monkeypatch, interval(10, 8), busy=[(interval(9, 30), interval(10, 7))])
    starts = {item.start_at for item in result}
    assert interval(10, 7) not in starts
    assert interval(10, 8) not in starts
    assert min(starts) == interval(10, 15)


@pytest.mark.anyio
async def test_telegram_date_picker_loads_one_range(monkeypatch):
    import app.api.v1.routes.messaging as messaging
    from unittest.mock import AsyncMock

    today = datetime.now(KYIV_TZ).date()
    horizon = messaging.booking_service_layer.availability_horizon_end_date()
    dates = [today + timedelta(days=offset) for offset in range(8)]
    range_call = AsyncMock(return_value=[(day, index != 1) for index, day in enumerate(dates)])
    day_call = AsyncMock(return_value=[])
    monkeypatch.setattr(messaging.booking_service_layer, "get_available_slots_range", range_call)
    monkeypatch.setattr(messaging.booking_service_layer, "get_available_slots", day_call)
    session = object()
    result = await messaging._available_visit_dates(session, master_id=4, service_ids=[3, 5])
    assert result == [day for index, day in enumerate(dates) if index != 1][:messaging.TELEGRAM_MAX_VISIT_DATE_BUTTONS]
    range_call.assert_awaited_once_with(session, master_id=4, start_date=today, end_date=horizon, service_ids=[3, 5])
    day_call.assert_not_awaited()


@pytest.mark.anyio
async def test_final_validation_rejects_published_time_beyond_horizon(monkeypatch):
    from fastapi import HTTPException
    from tests.test_booking import FakeSession

    freeze_clock(monkeypatch, interval(8))
    service = BookingServiceLayer()
    outside = service.availability_horizon_end_date() + timedelta(days=1)
    while outside.weekday() == 0:
        outside += timedelta(days=1)
    start, end = interval(10, day=outside), interval(11, day=outside)
    windows = [SimpleNamespace(start_at=start, end_at=end)]
    with pytest.raises(HTTPException) as error:
        await service.ensure_booking_within_availability(FakeSession(execute_values=[windows]), 1, start, end)
    assert error.value.status_code == 400
    assert "2 months" in error.value.detail
