"""Kyiv calendar buckets on isolated PostgreSQL under different session timezones."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.models.booking import BarberService, BookingServiceItem, BookingStatus, Master
from app.services.statistics import StatisticsService
from test_segments_integration import add_booking, add_customer, anyio_backend, database


KYIV = ZoneInfo("Europe/Kyiv")
SESSION_TIMEZONES = ("UTC", "Europe/Kyiv")
BOUNDARIES = (
    pytest.param("2026-01-02T00:15:00+02:00", id="winter-midnight"),
    pytest.param("2026-07-02T00:15:00+03:00", id="summer-midnight"),
    pytest.param("2026-01-05T00:15:00+02:00", id="monday-iso-week"),
    pytest.param("2025-12-29T00:15:00+02:00", id="december-iso-next-year"),
    pytest.param("2027-01-01T00:15:00+02:00", id="january-iso-previous-year"),
    pytest.param("2026-03-29T02:45:00+02:00", id="spring-before-jump"),
    pytest.param("2026-03-29T04:15:00+03:00", id="spring-after-jump"),
    pytest.param("2026-10-25T03:15:00+03:00", id="fall-first-fold"),
    pytest.param("2026-10-25T03:15:00+02:00", id="fall-second-fold"),
)


async def saved_booking(session, *, suffix, start_at, saved_price=75, master=None, service=None,
                        status=BookingStatus.completed, with_item=True):
    customer = await add_customer(session, suffix)
    start_utc = start_at.astimezone(UTC)
    booking = await add_booking(session, customer, start_utc + timedelta(minutes=30),
                                status=status, master=master, service=service)
    service = await session.get(BarberService, booking.service_id)
    service.price = 1000
    if with_item:
        booking.subtotal_amount = saved_price
        booking.total_amount = saved_price
        booking.promotion_discount_amount = 0
        booking.manual_discount_amount = 0
        session.add(BookingServiceItem(booking_id=booking.id, service_id=service.id,
                                      position=0, price_amount=saved_price))
    await session.flush()
    return booking


async def session_timezone(session, zone):
    # Transaction-local setting cannot escape into a subsequently pooled session.
    await session.execute(text("SELECT set_config('TimeZone', :zone, true)"), {"zone": zone})
    assert await session.scalar(text("SHOW TimeZone")) == zone


@pytest.mark.anyio
@pytest.mark.parametrize("zone", SESSION_TIMEZONES)
@pytest.mark.parametrize("instant", BOUNDARIES)
@pytest.mark.parametrize("bucket", ["day", "week"])
async def test_calendar_bucket_uses_kyiv_instead_of_session_timezone(database, zone, instant, bucket):
    local = datetime.fromisoformat(instant).astimezone(KYIV)
    async with database() as session:
        booking = await saved_booking(session, suffix=1, start_at=local)
        master_id = booking.master_id
        await session.commit()
        await session_timezone(session, zone)
        statistics = StatisticsService()
        start, end = statistics.month_bounds(local.year, local.month)
        method = getattr(statistics, f"_workload_by_{bucket}")
        rows = await method(session, start=start, end=end, barber_id=master_id)
        assert len(rows) == 1
        assert rows[0].completed_appointments == 1
        assert rows[0].revenue == Decimal("75.00")
        if bucket == "day":
            assert rows[0].date == local.date().isoformat()
        else:
            assert rows[0].week == local.isocalendar().week
            assert set(rows[0].model_dump()) == {"week", "completed_appointments", "revenue"}
        summary = await statistics._summary(session, start=start, end=end, barber_id=master_id)
        assert summary["total_income"] == Decimal("75.00")
        assert summary["completed_appointments"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("zone", SESSION_TIMEZONES)
@pytest.mark.parametrize("month", [1, 7])
async def test_monthly_day_normalization_keeps_local_midnight_and_period_edges(database, zone, month):
    statistics = StatisticsService()
    start, end = statistics.month_bounds(2026, month)
    async with database() as session:
        included = await saved_booking(session, suffix=1, start_at=start + timedelta(minutes=15))
        master = await session.get(Master, included.master_id)
        existing_service = await session.get(BarberService, included.service_id)
        await saved_booking(session, suffix=2, start_at=start - timedelta(minutes=15),
                            master=master, service=existing_service)
        await saved_booking(session, suffix=3, start_at=end, master=master, service=existing_service)
        await session.commit()
        await session_timezone(session, zone)
        response = await statistics.get_barber_monthly_statistics(
            session, year=2026, month=month, barber_id=master.id,
        )
        populated = [row for row in response.workload_by_day if row.completed_appointments]
        assert [(row.date, row.completed_appointments, row.revenue) for row in populated] == [
            (start.date().isoformat(), 1, Decimal("75.00")),
        ]
        assert response.total_income == Decimal("75.00")
        assert response.completed_appointments == 1
        assert sum(row.revenue for row in response.workload_by_day) == response.total_income
        assert sum(row.revenue for row in response.workload_by_week) == response.total_income
        assert response.best_revenue_day.date == start.date().isoformat()


@pytest.mark.anyio
@pytest.mark.parametrize("zone", SESSION_TIMEZONES)
@pytest.mark.parametrize("filter_master", [False, True])
async def test_timezone_buckets_preserve_master_filter_and_saved_totals(database, zone, filter_master):
    local = datetime(2026, 1, 5, 0, 15, tzinfo=KYIV)
    async with database() as session:
        first = await saved_booking(session, suffix=1, start_at=local)
        await saved_booking(session, suffix=2, start_at=local, saved_price=125)
        master_id = first.master_id if filter_master else None
        await session.commit()
        await session_timezone(session, zone)
        statistics = StatisticsService()
        start, end = statistics.month_bounds(2026, 1)
        expected_count = 1 if filter_master else 2
        expected_revenue = Decimal("75.00") if filter_master else Decimal("200.00")
        days = await statistics._workload_by_day(session, start=start, end=end, barber_id=master_id)
        weeks = await statistics._workload_by_week(session, start=start, end=end, barber_id=master_id)
        assert [(row.date, row.completed_appointments, row.revenue) for row in days] == [
            (local.date().isoformat(), expected_count, expected_revenue),
        ]
        assert [(row.week, row.completed_appointments, row.revenue) for row in weeks] == [
            (local.isocalendar().week, expected_count, expected_revenue),
        ]
        summary = await statistics._summary(session, start=start, end=end, barber_id=master_id)
        assert summary["total_income"] == expected_revenue
        assert summary["completed_appointments"] == expected_count


@pytest.mark.anyio
@pytest.mark.parametrize("zone", SESSION_TIMEZONES)
async def test_timezone_change_preserves_inner_join_legacy_and_completed_status_rules(database, zone):
    local = datetime(2026, 1, 5, 12, tzinfo=KYIV)
    async with database() as session:
        completed = await saved_booking(session, suffix=1, start_at=local)
        master = await session.get(Master, completed.master_id)
        existing_service = await session.get(BarberService, completed.service_id)
        await saved_booking(session, suffix=2, start_at=local, master=master,
                            service=existing_service, with_item=False)
        await saved_booking(session, suffix=3, start_at=local, master=master,
                            service=existing_service, status=BookingStatus.cancelled, saved_price=500)
        await session.commit()
        await session_timezone(session, zone)
        statistics = StatisticsService()
        start, end = statistics.month_bounds(2026, 1)
        days = await statistics._workload_by_day(session, start=start, end=end, barber_id=master.id)
        weeks = await statistics._workload_by_week(session, start=start, end=end, barber_id=master.id)
        assert [(row.date, row.completed_appointments, row.revenue) for row in days] == [
            (local.date().isoformat(), 1, Decimal("75.00")),
        ]
        assert [(row.week, row.completed_appointments, row.revenue) for row in weeks] == [
            (local.isocalendar().week, 1, Decimal("75.00")),
        ]
        summary = await statistics._summary(session, start=start, end=end, barber_id=master.id)
        assert summary["total_income"] == Decimal("75.00")
        assert summary["completed_appointments"] == 1
        assert summary["cancelled_appointments"] == 1
