"""Real PostgreSQL proof that optional funnel telemetry cannot undo a booking."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from app.models.booking import BarberService, Booking, Master, MasterAvailabilityWindow
from app.models.booking_funnel import BookingFunnelEvent
from app.schemas.booking import PublicBookingCreate
from app.services.booking import BookingServiceLayer
from tests.test_booking_alternatives import availability_database  # noqa: F401


KYIV = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def next_open_day() -> datetime:
    value = datetime.now(KYIV).date() + timedelta(days=1)
    while value.weekday() == 0:
        value += timedelta(days=1)
    return datetime(value.year, value.month, value.day, 10, tzinfo=KYIV)


@pytest.mark.anyio
async def test_funnel_trigger_failure_keeps_booking_durable(availability_database) -> None:
    """A trigger failure is contained by the telemetry savepoint in a disposable schema."""
    _engine, sessions = availability_database
    service = BookingServiceLayer()
    start_at = next_open_day()
    day_start, day_end = service.day_bounds(start_at.date())

    async with sessions() as setup:
        master = Master(full_name="Telemetry resilience master", is_active=True)
        setup.add(master)
        await setup.flush()
        barber_service = BarberService(
            master_id=master.id,
            name="Telemetry resilience service",
            duration_minutes=30,
            price=100,
            is_active=True,
        )
        setup.add_all([
            barber_service,
            MasterAvailabilityWindow(master_id=master.id, start_at=day_start, end_at=day_end),
        ])
        await setup.commit()

    async with sessions() as setup:
        await setup.execute(text("""
            CREATE FUNCTION reject_funnel_telemetry() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'funnel telemetry intentionally rejected';
            END;
            $$ LANGUAGE plpgsql
        """))
        await setup.execute(text("""
            CREATE TRIGGER reject_funnel_telemetry_insert
            BEFORE INSERT ON booking_funnel_events
            FOR EACH ROW EXECUTE FUNCTION reject_funnel_telemetry()
        """))
        await setup.commit()

    async with sessions() as session:
        booking = await service.create_public_booking(
            session,
            PublicBookingCreate(
                master_id=master.id,
                service_id=barber_service.id,
                customer_name="Telemetry Customer",
                customer_phone="+380501234567",
                start_at=start_at,
                funnel_session_id="telemetry-resilience-session-001",
            ),
            record_funnel_success=True,
        )

    async with sessions() as verify:
        assert (await verify.scalars(select(Booking))).one().id == booking.id
        assert await verify.scalar(select(func.count()).select_from(BookingFunnelEvent)) == 0

        # The connection remains usable after the failed nested analytics insert.
        second = await BookingServiceLayer().create_public_booking(
            verify,
            PublicBookingCreate(
                master_id=master.id,
                service_id=barber_service.id,
                customer_name="Follow-up Customer",
                customer_phone="+380501234568",
                start_at=start_at + timedelta(hours=1),
            ),
            record_funnel_success=False,
        )
        assert second.id != booking.id
