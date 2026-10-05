"""Interval rules against disposable PostgreSQL; never use the configured app DB."""
from datetime import UTC, datetime, timedelta
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.booking import BarberService, Booking, Master, MasterAvailabilityWindow, MasterTimeBlock
from app.models.customer import Customer
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest
from app.schemas.booking import PublicBookingCreate, PublicBookingQuoteRequest
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.services.booking import BookingServiceLayer, KYIV_TZ
from app.services.booking_alternatives import BookingAlternativesService
from tests.test_booking_alternatives import availability_database  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


def target_start():
    day = datetime.now(KYIV_TZ).date() + timedelta(days=2)
    while day.weekday() == 0:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 10, tzinfo=KYIV_TZ)


async def seed(sessions, *, duration=30, windows=None):
    start = target_start()
    async with sessions() as session:
        master = Master(full_name="Interval fixture", is_active=True)
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Interval service", duration_minutes=duration,
                                price=100, is_active=True)
        session.add(service)
        session.add_all([MasterAvailabilityWindow(master_id=master.id, start_at=left, end_at=right)
                         for left, right in (windows or [(start, start + timedelta(hours=2))])])
        await session.commit()
    return master.id, service.id, start


def payload(master_id, service_id, start, index=1):
    return PublicBookingCreate(master_id=master_id, service_ids=[service_id], start_at=start,
                               customer_name="Interval Customer", customer_phone=f"+38050123{index:04d}")


@pytest.mark.anyio
@pytest.mark.parametrize("separation,accepted", [(0, True), (-15, True), (1, False)])
async def test_window_union_offered_and_created_without_rewriting_rows(availability_database, separation, accepted):
    _, sessions = availability_database
    start = target_start()
    master_id, service_id, _ = await seed(sessions, duration=60, windows=[
        (start, start + timedelta(hours=1)),
        (start + timedelta(hours=1, minutes=separation), start + timedelta(hours=2)),
    ])
    selected = start + timedelta(minutes=30)
    async with sessions() as session:
        service = BookingServiceLayer()
        slots = await service.get_available_slots(session, master_id, service_id, start.date())
        assert (selected in {slot.start_at for slot in slots}) is accepted
        assert len(slots) == len({slot.start_at for slot in slots})
        assert (await service.get_available_slots_range(
            session, master_id, start.date(), start.date(), service_id=service_id)) == [(start.date(), True)]
        if accepted:
            booking = await service.create_public_booking(session, payload(master_id, service_id, selected.astimezone(UTC)))
            assert booking.start_at == selected
            assert booking.end_at == selected + timedelta(hours=1)
        else:
            with pytest.raises(HTTPException) as error:
                await service.create_public_booking(session, payload(master_id, service_id, selected))
            assert error.value.status_code == 409
    async with sessions() as verify:
        assert await verify.scalar(select(func.count()).select_from(MasterAvailabilityWindow)) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["block", "booking", "sent", "delivered", "expired", "expired_status"])
async def test_busy_union_free_start_holds_quote_and_final_create(availability_database, kind):
    _, sessions = availability_database
    master_id, service_id, start = await seed(sessions)
    end = start + timedelta(minutes=7)
    async with sessions() as setup:
        # An overlapping shorter block must not leak a false 10:03 free start.
        setup.add(MasterTimeBlock(master_id=master_id, start_at=start, end_at=start + timedelta(minutes=3)))
        if kind == "block":
            setup.add(MasterTimeBlock(master_id=master_id, start_at=start + timedelta(minutes=1), end_at=end))
        elif kind == "booking":
            setup.add(Booking(master_id=master_id, service_id=service_id, start_at=start, end_at=end,
                              customer_name="Busy", customer_phone="+380501234599"))
        else:
            customer = Customer(phone="+380501234598", name="Hold", is_active=True)
            setup.add(customer)
            await setup.flush()
            request = WaitlistRequest(customer_id=customer.id, preferred_master_id=master_id,
                                      desired_date=start.date(), duration_minutes=30, notification_consent=True,
                                      expires_at=start, cancel_token_hash="cancel", dedup_key_hash="dedup")
            setup.add(request)
            await setup.flush()
            setup.add(WaitlistOffer(request_id=request.id, master_id=master_id, start_at=start, end_at=end,
                                   token_hash="hold", scheduled_at=datetime.now(UTC),
                                   status=(WaitlistOfferStatus.expired if kind == "expired_status" else
                                           WaitlistOfferStatus.delivered if kind == "delivered" else WaitlistOfferStatus.sent),
                                   expires_at=(datetime.now(UTC) - timedelta(seconds=1) if kind == "expired" else start)))
        await setup.commit()
    selected = start + timedelta(minutes=3) if kind.startswith("expired") else end
    async with sessions() as session:
        service = BookingServiceLayer()
        slots = await service.get_available_slots(session, master_id, service_id, start.date())
        starts = {slot.start_at for slot in slots}
        assert selected in starts
        assert start + timedelta(minutes=15) in starts
        assert start + timedelta(minutes=22) not in starts
        if not kind.startswith("expired"):
            assert start + timedelta(minutes=3) not in starts
        # Quote is read-only pricing, and must retain the same exact instant.
        from app.api.v1.routes.bookings import quote_public_booking
        quote = await quote_public_booking(PublicBookingQuoteRequest(
            master_id=master_id, service_ids=[service_id], start_at=selected), session)
        assert quote.subtotal_amount == 100
        booking = await service.create_public_booking(session, payload(master_id, service_id, selected))
        assert booking.start_at == selected
        assert booking.end_at == selected + timedelta(minutes=30)


@pytest.mark.anyio
@pytest.mark.parametrize("off_grid", [False, True])
async def test_concurrent_creation_for_newly_available_slots(availability_database, off_grid):
    _, sessions = availability_database
    start = target_start()
    master_id, service_id, _ = await seed(sessions, duration=60, windows=[
        (start, start + timedelta(hours=1)), (start + timedelta(hours=1), start + timedelta(hours=2))])
    selected = start + timedelta(minutes=7 if off_grid else 30)
    if off_grid:
        async with sessions() as setup:
            setup.add(MasterTimeBlock(master_id=master_id, start_at=start, end_at=selected))
            await setup.commit()
    async def attempt(index):
        async with sessions() as session:
            try:
                await BookingServiceLayer().create_public_booking(session, payload(master_id, service_id, selected, index))
                return 201
            except HTTPException as error:
                return error.status_code
    assert sorted(await asyncio.gather(attempt(1), attempt(2))) == [201, 409]
    async with sessions() as verify:
        assert await verify.scalar(select(func.count()).select_from(Booking)) == 1


@pytest.mark.anyio
async def test_redirect_multiservice_alternatives_keep_public_identity_and_physical_dedup(availability_database):
    _, sessions = availability_database
    physical_id, physical_service_id, start = await seed(sessions, windows=[
        (target_start(), target_start() + timedelta(minutes=30)),
        (target_start() + timedelta(minutes=30), target_start() + timedelta(hours=2))])
    async with sessions() as setup:
        physical = await setup.get(Master, physical_id)
        physical.show_on_master_block = False
        technical_second = BarberService(master_id=physical_id, name="Second service", duration_minutes=30,
                                          price=150, is_active=True)
        public = Master(full_name="Public barber", is_active=True, booking_redirect_master_id=physical_id)
        alias = Master(full_name="Public alias", is_active=True, booking_redirect_master_id=physical_id)
        setup.add_all([technical_second, public, alias])
        await setup.flush()
        selected_services = []
        for master in (public, alias):
            services = [BarberService(master_id=master.id, name="Interval service", duration_minutes=30, price=100, is_active=True),
                        BarberService(master_id=master.id, name="Second service", duration_minutes=30, price=150, is_active=True)]
            setup.add_all(services)
            await setup.flush()
            if master is public:
                selected_services = [item.id for item in services]
        # A window on the public alias cannot fill a gap in the physical calendar.
        setup.add(MasterAvailabilityWindow(master_id=public.id, start_at=start - timedelta(minutes=30), end_at=start))
        await setup.commit()
    async with sessions() as session:
        service = BookingServiceLayer()
        result = await service.get_available_slots_for_range(session, public.id, start.date(), start.date(),
                                                             service_ids=selected_services)
        assert result.booking_master_id == physical_id
        selected = start + timedelta(minutes=15)
        assert selected in {slot.start_at for slot in result.slots_by_date[start.date()]}
        assert start - timedelta(minutes=15) not in {slot.start_at for slot in result.slots_by_date[start.date()]}
        alternatives = await BookingAlternativesService().find(session, BookingAlternativesRequest(
            master_id=public.id, service_ids=selected_services, desired_date=start.date() - timedelta(days=1),
            duration_minutes=60, page_size=100))
        assert any(slot.start_at == selected for slot in alternatives.same_master)
        assert all(slot.master.id == public.id for slot in alternatives.same_master)
        all_slots = alternatives.same_master + alternatives.other_masters
        assert len(all_slots) == len({slot.start_at for slot in all_slots})
        with pytest.raises(HTTPException) as error:
            await service.create_public_booking(session, PublicBookingCreate(
                master_id=public.id, service_ids=selected_services, start_at=selected, duration_minutes=30,
                customer_name="Strict", customer_phone="+380501234597"))
        assert error.value.status_code == 400
    async with sessions() as session:
        booking = await BookingServiceLayer().create_public_booking(session, PublicBookingCreate(
            master_id=public.id, service_ids=selected_services, start_at=selected,
            customer_name="Redirect", customer_phone="+380501234596"))
        assert booking.master_id == physical_id
        assert booking.redirected_from_master_id == public.id
        assert booking.end_at - booking.start_at == timedelta(hours=1)
        await session.refresh(booking, attribute_names=["service_items"])
        assert {item.service_id for item in booking.service_items} == {physical_service_id, technical_second.id}


@pytest.mark.anyio
async def test_imported_window_beyond_horizon_cannot_be_booked(availability_database):
    _, sessions = availability_database
    service = BookingServiceLayer()
    day = service.availability_horizon_end_date() + timedelta(days=1)
    while day.weekday() == 0:
        day += timedelta(days=1)
    start = datetime(day.year, day.month, day.day, 10, tzinfo=KYIV_TZ)
    master_id, service_id, _ = await seed(sessions, windows=[(start, start + timedelta(hours=1))])
    async with sessions() as session:
        with pytest.raises(HTTPException) as error:
            await service.create_public_booking(session, payload(master_id, service_id, start))
        assert error.value.status_code == 400
        assert "2 months" in error.value.detail
    async with sessions() as verify:
        assert await verify.scalar(select(func.count()).select_from(Booking)) == 0
