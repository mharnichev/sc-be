"""Independent PostgreSQL scheduling races for writers beyond create-vs-create."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.routes.bookings import update_my_booking
from app.models.booking import (
    BarberService,
    Booking,
    BookingStatus,
    Master,
    MasterAvailabilityWindow,
)
from app.models.customer import Customer
from app.models.waitlist import (
    WaitlistOffer,
    WaitlistOfferStatus,
    WaitlistRequest,
    WaitlistStatus,
)
from app.schemas.booking import BookingUpdate, PublicBookingCreate
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.customer_activity import CustomerActivityService
from app.services.waitlist_offers import WaitlistOfferService
from tests import test_booking_alternatives as alternatives_tests

availability_database = alternatives_tests.availability_database


@pytest.fixture
def anyio_backend():
    return "asyncio"


def future_start() -> datetime:
    day = datetime.now(KYIV_TZ).date() + timedelta(days=2)
    while day.weekday() == 0:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 10, tzinfo=KYIV_TZ)


async def calendar(sessions) -> tuple[int, int, datetime]:
    start = future_start()
    async with sessions() as session:
        master = Master(full_name="Concurrency master", is_active=True)
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Concurrency service", duration_minutes=30,
                                price=100, is_active=True)
        session.add_all([
            service,
            MasterAvailabilityWindow(master_id=master.id, start_at=start - timedelta(minutes=30),
                                     end_at=start + timedelta(hours=4)),
        ])
        await session.commit()
    return master.id, service.id, start


def booking_payload(master_id: int, service_id: int, start_at: datetime, index: int) -> PublicBookingCreate:
    return PublicBookingCreate(
        master_id=master_id,
        service_ids=[service_id],
        start_at=start_at,
        customer_name=f"Race Customer {index}",
        customer_phone=f"+38067123{index:04d}",
    )


async def synchronized_pair(left, right):
    ready = asyncio.Event()
    arrivals = 0

    async def run(operation):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        return await operation()

    return await asyncio.wait_for(asyncio.gather(run(left), run(right)), timeout=20)


@pytest.mark.anyio
async def test_booking_vs_master_reschedule_serializes_target_slot(availability_database):
    _, sessions = availability_database
    master_id, service_id, old_start = await calendar(sessions)
    target_start = old_start + timedelta(hours=1)
    async with sessions() as session:
        original = await BookingServiceLayer().create_public_booking(
            session, booking_payload(master_id, service_id, old_start, 1),
        )
        booking_id = original.id

    async def create_at_target():
        async with sessions() as session:
            try:
                await BookingServiceLayer().create_public_booking(
                    session, booking_payload(master_id, service_id, target_start, 2),
                )
                return 201
            except HTTPException as error:
                return error.status_code

    async def reschedule_to_target():
        async with sessions() as session:
            try:
                await update_my_booking(
                    booking_id,
                    BookingUpdate(start_at=target_start, end_at=target_start + timedelta(minutes=30)),
                    current_master=await session.get(Master, master_id),
                    session=session,
                    background_tasks=None,
                )
                return 200
            except HTTPException as error:
                return error.status_code

    outcomes = await synchronized_pair(create_at_target, reschedule_to_target)
    assert sorted(outcomes) == [200, 409] or sorted(outcomes) == [201, 409]
    async with sessions() as verify:
        rows = list((await verify.scalars(select(Booking).where(Booking.status == BookingStatus.confirmed))).all())
        target_rows = [item for item in rows if item.start_at == target_start]
        assert len(target_rows) == 1
        assert len(rows) in {1, 2}
        assert all(item.end_at - item.start_at == timedelta(minutes=30) for item in rows)
        original = await verify.get(Booking, booking_id)
        assert original.start_at in {old_start, target_start}


@pytest.mark.anyio
async def test_waitlist_claim_vs_booking_leaves_one_claimed_offer_and_one_booking(availability_database):
    _, sessions = availability_database
    master_id, service_id, start = await calendar(sessions)
    token = "concurrency-waitlist-token"
    async with sessions() as session:
        customer = Customer(phone="+380671239900", name="Waitlist Claim", is_active=True)
        session.add(customer)
        await session.flush()
        request = WaitlistRequest(
            customer_id=customer.id,
            preferred_master_id=master_id,
            desired_date=start.date(),
            duration_minutes=30,
            notification_consent=True,
            expires_at=start,
            cancel_token_hash="waitlist-cancel-race",
            dedup_key_hash="waitlist-dedup-race",
            status=WaitlistStatus.offered,
        )
        request.services.append(await session.get(BarberService, service_id))
        session.add(request)
        await session.flush()
        offer = WaitlistOffer(
            request_id=request.id,
            master_id=master_id,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            token_hash=WaitlistOfferService._hash(token),
            status=WaitlistOfferStatus.sent,
            expires_at=start,
            scheduled_at=datetime.now(UTC),
        )
        session.add(offer)
        await session.commit()
        offer_id, request_id = offer.id, request.id

    async def claim_offer():
        async with sessions() as session:
            try:
                await WaitlistOfferService().claim(session, token)
                return 201
            except HTTPException as error:
                return error.status_code

    async def book_offer_slot():
        async with sessions() as session:
            try:
                await BookingServiceLayer().create_public_booking(
                    session, booking_payload(master_id, service_id, start, 3),
                )
                return 201
            except HTTPException as error:
                return error.status_code

    outcomes = await synchronized_pair(claim_offer, book_offer_slot)
    assert sorted(outcomes) == [201, 409]
    async with sessions() as verify:
        rows = list((await verify.scalars(select(Booking).where(Booking.status == BookingStatus.confirmed))).all())
        offer = await verify.get(WaitlistOffer, offer_id)
        request = await verify.get(WaitlistRequest, request_id)
        assert len(rows) == 1 and rows[0].start_at == start
        assert offer.status == WaitlistOfferStatus.claimed
        assert request.status == WaitlistStatus.booked


@pytest.mark.anyio
async def test_cancelled_booking_slot_can_be_rebooked_with_persisted_state(availability_database):
    _, sessions = availability_database
    master_id, service_id, start = await calendar(sessions)
    async with sessions() as session:
        original = await BookingServiceLayer().create_public_booking(
            session, booking_payload(master_id, service_id, start, 4),
        )
        original_id, customer_id, public_id = original.id, original.customer_id, original.public_id

    async with sessions() as session:
        customer = await session.get(Customer, customer_id)
        cancelled, freed = await CustomerActivityService().cancel_booking(session, customer, public_id)
        assert cancelled.id == original_id and freed.start_at == start

    async with sessions() as session:
        replacement = await BookingServiceLayer().create_public_booking(
            session, booking_payload(master_id, service_id, start, 5),
        )
        replacement_id = replacement.id

    async with sessions() as verify:
        original = await verify.get(Booking, original_id)
        replacement = await verify.get(Booking, replacement_id)
        active = list((await verify.scalars(select(Booking).where(Booking.status == BookingStatus.confirmed))).all())
        assert original.status == BookingStatus.cancelled and original.cancelled_at is not None
        assert replacement.status == BookingStatus.confirmed and replacement.start_at == start
        assert [item.id for item in active] == [replacement_id]
