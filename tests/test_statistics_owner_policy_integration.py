"""Trusted legacy totals and actual completed service-item counts on PostgreSQL."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.models.booking import BarberService, BookingServiceItem, BookingStatus, Master
from app.services.statistics import StatisticsService
from test_segments_integration import add_booking, add_customer, anyio_backend, database


@pytest.mark.anyio
@pytest.mark.parametrize("session_timezone", ["UTC", "Europe/Kyiv"])
async def test_monthly_legacy_totals_items_and_master_scopes_agree(database, session_timezone):
    local = datetime(2026, 1, 5, 0, 15, tzinfo=ZoneInfo("Europe/Kyiv"))
    statistics = StatisticsService()
    async with database() as session:
        master = Master(full_name="Policy master")
        session.add(master)
        await session.flush()
        first_service = BarberService(master_id=master.id, name="Haircut", duration_minutes=30, price=9999)
        second_service = BarberService(master_id=master.id, name="Beard", duration_minutes=30, price=8888)
        session.add_all([first_service, second_service])
        await session.flush()
        customer = await add_customer(session, 1)
        priced = await add_booking(session, customer, local + timedelta(minutes=30),
                                   master=master, service=first_service)
        priced.subtotal_amount = 100
        priced.total_amount = 80
        session.add_all([
            BookingServiceItem(booking_id=priced.id, service_id=first_service.id, position=0, price_amount=60),
            BookingServiceItem(booking_id=priced.id, service_id=second_service.id, position=1, price_amount=40),
        ])
        legacy = await add_booking(session, await add_customer(session, 2), local + timedelta(hours=1),
                                  master=master, service=first_service)
        legacy.total_amount = 70
        legacy.subtotal_amount = 100
        zero = await add_booking(session, await add_customer(session, 6), local + timedelta(hours=1, minutes=15),
                                master=master, service=first_service)
        zero.total_amount = 0
        # No trusted snapshot: retain historical financial exclusion, no current-price invention.
        await add_booking(session, await add_customer(session, 3), local + timedelta(hours=2),
                          master=master, service=first_service)
        cancelled = await add_booking(session, await add_customer(session, 4), local + timedelta(hours=3),
                                     status=BookingStatus.cancelled, master=master, service=first_service)
        cancelled.total_amount = 1000
        outside = await add_booking(session, await add_customer(session, 5), local + timedelta(hours=4))
        outside.total_amount = 30
        await session.commit()
        await session.execute(text("SELECT set_config('TimeZone', :zone, true)"), {"zone": session_timezone})
        monthly = await statistics.get_barber_monthly_statistics(session, year=2026, month=1, barber_id=master.id)
        assert monthly.total_income == Decimal("150.00")
        assert (monthly.completed_appointments, monthly.completed_visits, monthly.total_services_performed) == (3, 3, 3)
        assert monthly.completed_service_items == 2
        assert monthly.unique_clients == 3
        assert monthly.average_check_per_appointment == Decimal("50.00")
        assert monthly.cancelled_appointments == 1
        assert [(row.count, row.revenue) for row in monthly.revenue_by_service] == [(1, Decimal("48.00")), (1, Decimal("32.00"))]
        assert sum(row.revenue for row in monthly.workload_by_day) == monthly.total_income
        assert sum(row.revenue for row in monthly.workload_by_week) == monthly.total_income
        assert monthly.best_revenue_day.date == "2026-01-05"
        assert monthly.best_revenue_day.completed_appointments == 3
        comparison = await statistics.get_barbers_comparison(session, year=2026, month=1)
        item = next(item for item in comparison.barbers if item.barber.id == master.id)
        assert (item.revenue, item.completed_appointments, item.unique_clients) == (Decimal("150.00"), 3, 3)
        admin = await statistics.get_admin_monthly_statistics(session, year=2026, month=1, barber_id=master.id)
        assert admin.aggregate.total_income == Decimal("150.00")
        assert admin.total_barbershop_monthly_revenue == Decimal("180.00")
        assert admin.total_completed_appointments == 4
        assert admin.aggregate.completed_service_items == 2
