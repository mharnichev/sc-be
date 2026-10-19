from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.booking import BarberService, Master, MasterAvailabilityWindow
from app.schemas.booking import PublicBookingCreate
from app.services.booking import KYIV_TZ, BookingServiceLayer
from tests.test_booking_alternatives import availability_database


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_monday_requires_published_window_and_preserves_conflict_guard(availability_database):
    _, sessions = availability_database
    service = BookingServiceLayer()
    today = datetime.now(KYIV_TZ).date()
    monday = today + timedelta(days=7 - today.weekday())
    start, end = service.day_bounds(monday)
    async with sessions() as session:
        master = Master(full_name="Synthetic Monday master", is_active=True)
        session.add(master)
        await session.flush()
        haircut = BarberService(master_id=master.id, name="Synthetic Monday cut",
                                duration_minutes=30, price=100, is_active=True)
        session.add(haircut)
        await session.commit()
        master_id, service_id = master.id, haircut.id
        assert await service.get_available_slots(session, master_id, service_id, monday) == []

    def payload(phone):
        return PublicBookingCreate(master_id=master_id, service_ids=[service_id],
                                   start_at=start, customer_name="Synthetic Monday customer",
                                   customer_phone=phone)

    async with sessions() as session:
        with pytest.raises(HTTPException) as unavailable:
            await service.create_public_booking(session, payload("+380501110001"))
        assert unavailable.value.status_code == 409

    async with sessions() as session:
        session.add(MasterAvailabilityWindow(master_id=master_id, start_at=start, end_at=end))
        await session.commit()
        slots = await service.get_available_slots(session, master_id, service_id, monday)
        assert slots[0].start_at == start
        booking = await service.create_public_booking(session, payload("+380501110001"))
        assert booking.start_at == start

    async with sessions() as session:
        with pytest.raises(HTTPException) as occupied:
            await service.create_public_booking(session, payload("+380501110002"))
        assert occupied.value.status_code == 409
        remaining = await service.get_available_slots(session, master_id, service_id, monday)
        assert remaining and remaining[0].start_at == start + timedelta(minutes=30)
        assert all(slot.start_at >= start + timedelta(minutes=30) for slot in remaining)
