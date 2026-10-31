"""Local PostgreSQL only; no application lifespan or real message transport."""
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from app.models.booking import BarberService, Booking, BookingStatus
from app.models.waitlist import WaitlistOffer, WaitlistRequest, WaitlistStatus
from app.services.waitlist_offers import WaitlistOfferService
from tests.test_waitlist_expiry_concurrency_integration import blocked_or_finished, bounded_transaction, clock, seed  # noqa: F401
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


async def free_fixture(database, clock):
    row = (await seed(database, clock))[0]
    async with database() as session:
        await session.execute(delete(WaitlistOffer))
        request = await session.get(WaitlistRequest, row['request_id'])
        request.status = WaitlistStatus.active
        await session.commit()
    return row


@pytest.mark.anyio
async def test_concurrent_and_repeated_freed_slot_has_one_pending_offer(database, clock, monkeypatch):
    import asyncio

    row = await free_fixture(database, clock)
    service = WaitlistOfferService()
    monkeypatch.setattr(service, '_scheduled_send_at', lambda now: now + timedelta(minutes=30))

    async def forbidden_send(*args, **kwargs):
        pytest.fail('No transport should run for a future pending offer')

    monkeypatch.setattr(service, '_send_offer', forbidden_send)

    first_locked, release_first, second_started = (asyncio.Event() for _ in range(3))
    pids = {}
    get_master = service.booking_service.get_active_master_with_services

    async def gated_master(session, *args, **kwargs):
        master = await get_master(session, *args, **kwargs)
        if session.info.get('incident_role') == 'first':
            first_locked.set()
            await asyncio.wait_for(release_first.wait(), timeout=10)
        return master

    monkeypatch.setattr(service.booking_service, 'get_active_master_with_services', gated_master)

    async def offer(role):
        async with database() as session:
            session.info['incident_role'] = role
            pids[role] = await bounded_transaction(session)
            if role == 'second':
                second_started.set()
            return await service.offer_slot(session, master_id=row['master_id'],
                                            start_at=row['start_at'], end_at=row['end_at'])

    first = asyncio.create_task(offer('first'))
    second = None
    try:
        await asyncio.wait_for(first_locked.wait(), timeout=5)
        second = asyncio.create_task(offer('second'))
        await asyncio.wait_for(second_started.wait(), timeout=5)
        assert await blocked_or_finished(database, pids['first'], pids['second'], second)
        release_first.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
        assert sum(result is not None for result in results) == 1
    finally:
        release_first.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
    assert await offer('repeat') is None
    async with database() as session:
        assert await session.scalar(select(func.count(WaitlistOffer.id))) == 1
        request = await session.get(WaitlistRequest, row['request_id'])
        assert request.status == WaitlistStatus.offered


@pytest.mark.anyio
async def test_reoccupied_snapshot_rolls_back_and_keeps_committed_booking(database, clock, monkeypatch):
    row = await free_fixture(database, clock)
    async with database() as session:
        request = await session.get(WaitlistRequest, row['request_id'])
        # The rescheduled booking overlaps the old snapshot by 15 minutes.
        service_id = await session.scalar(select(BarberService.id).where(BarberService.master_id == row['master_id']))
        booking = Booking(master_id=row['master_id'], service_id=service_id, customer_id=request.customer_id,
                          customer_name='Synthetic booking', customer_phone='+380500000000',
                          start_at=row['start_at'] + timedelta(minutes=15),
                          end_at=row['end_at'] + timedelta(minutes=15),
                          status=BookingStatus.confirmed)
        session.add(booking)
        await session.commit()
        booking_id = booking.id
    service = WaitlistOfferService()

    async def forbidden_send(*args, **kwargs):
        pytest.fail('An occupied slot must not send a message')

    monkeypatch.setattr(service, '_send_offer', forbidden_send)
    async with database() as session:
        assert await service.offer_slot(session, master_id=row['master_id'],
                                        start_at=row['start_at'], end_at=row['end_at']) is None
        assert not session.in_transaction()
    async with database() as session:
        assert await session.get(Booking, booking_id) is not None
        assert await session.scalar(select(func.count(WaitlistOffer.id))) == 0
        request = await session.get(WaitlistRequest, row['request_id'])
        assert request.status == WaitlistStatus.active


@pytest.mark.anyio
async def test_partial_reschedule_released_fragment_creates_offer(database, clock, monkeypatch):
    from fastapi import BackgroundTasks
    from app.api.v1.routes.bookings import schedule_rescheduled_waitlist_offers
    from app.services.waitlist_offers import FreedBookingSlot

    row = await free_fixture(database, clock)
    start = row['start_at']
    async with database() as session:
        request = await session.get(WaitlistRequest, row['request_id'])
        service_id = await session.scalar(select(BarberService.id).where(BarberService.master_id == row['master_id']))
        moved = Booking(master_id=row['master_id'], service_id=service_id, customer_id=request.customer_id,
                        customer_name='Synthetic moved booking', customer_phone='+380500000000',
                        start_at=start + timedelta(minutes=30), end_at=start + timedelta(minutes=90),
                        status=BookingStatus.confirmed)
        session.add(moved)
        await session.commit()
        booking_id = moved.id
    tasks = BackgroundTasks()
    original = FreedBookingSlot(row['master_id'], start, start + timedelta(minutes=60), booking_id, row['master_id'])
    schedule_rescheduled_waitlist_offers(tasks, original, start + timedelta(minutes=30), start + timedelta(minutes=90))
    assert len(tasks.tasks) == 1
    slot = tasks.tasks[0].args[0]
    assert (slot.start_at, slot.end_at) == (start, start + timedelta(minutes=30))
    service = WaitlistOfferService()
    monkeypatch.setattr(service, '_scheduled_send_at', lambda now: now + timedelta(minutes=30))

    async def forbidden_send(*args, **kwargs):
        pytest.fail('Pending offer must not use transport')

    monkeypatch.setattr(service, '_send_offer', forbidden_send)
    async with database() as session:
        offer = await service.offer_slot(session, master_id=slot.master_id, start_at=slot.start_at,
                                        end_at=slot.end_at, source_booking_id=slot.source_booking_id)
        assert offer is not None
        assert offer.source_booking_id == booking_id
        assert offer.request_id == row['request_id']
    async with database() as session:
        assert await session.scalar(select(func.count(WaitlistOffer.id))) == 1
        assert (await session.get(WaitlistRequest, row['request_id'])).status == WaitlistStatus.offered


@pytest.mark.anyio
async def test_stale_snapshot_keeps_request_eligible_when_slot_is_freed_again(database, clock, monkeypatch):
    row = await free_fixture(database, clock)
    service = WaitlistOfferService()
    monkeypatch.setattr(service, '_scheduled_send_at', lambda now: now + timedelta(minutes=30))

    async def forbidden_send(*args, **kwargs):
        pytest.fail('Pending or occupied offers must not use transport')

    monkeypatch.setattr(service, '_send_offer', forbidden_send)
    async with database() as session:
        service_id = await session.scalar(select(BarberService.id).where(BarberService.master_id == row['master_id']))
        occupying = Booking(master_id=row['master_id'], service_id=service_id,
                            customer_name='Synthetic occupying booking', customer_phone='+380500000001',
                            start_at=row['start_at'], end_at=row['end_at'], status=BookingStatus.confirmed)
        session.add(occupying)
        await session.commit()
        occupying_id = occupying.id
    async with database() as session:
        assert await service.offer_slot(session, master_id=row['master_id'], start_at=row['start_at'],
                                        end_at=row['end_at']) is None
    async with database() as session:
        request = await session.get(WaitlistRequest, row['request_id'])
        assert request.status == WaitlistStatus.active
        assert await session.scalar(select(func.count(WaitlistOffer.id))) == 0
        occupying = await session.get(Booking, occupying_id)
        await session.delete(occupying)
        await session.commit()
    async with database() as session:
        offer = await service.offer_slot(session, master_id=row['master_id'], start_at=row['start_at'],
                                        end_at=row['end_at'])
        assert offer is not None
        assert offer.request_id == row['request_id']
    async with database() as session:
        assert await session.scalar(select(func.count(WaitlistOffer.id))) == 1
        assert (await session.get(WaitlistRequest, row['request_id'])).status == WaitlistStatus.offered
