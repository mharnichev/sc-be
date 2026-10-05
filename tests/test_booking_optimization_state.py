"""Authoritative state and independent transactions against opt-in disposable PostgreSQL."""
import asyncio
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

import app.services.booking as booking_module
from app.api.v1.routes.bookings import quote_public_booking
from app.models.booking import (
    BarberService,
    BaseService,
    Booking,
    BookingSource,
    Master,
    MasterAvailabilityWindow,
)
from app.models.customer import Customer
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest
from app.schemas.booking import PublicBookingCreate, PublicBookingQuoteRequest
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.booking_alternatives import BookingAlternativesService
from tests import test_booking_alternatives as availability_tests

availability_database = availability_tests.availability_database


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def catalog(sessions):
    day = datetime.now(KYIV_TZ).date() + timedelta(days=2)
    while day.weekday() == 0:
        day += timedelta(days=1)
    start = datetime(day.year, day.month, day.day, 10, tzinfo=KYIV_TZ)
    async with sessions() as session:
        base = BaseService(name="Optimization synthetic cut", duration_minutes=30, price=100, is_active=True)
        technical = Master(full_name="Technical", is_active=True, show_on_master_block=False)
        session.add_all([base, technical])
        await session.flush()
        public = Master(full_name="Public A", is_active=True, booking_redirect_master_id=technical.id)
        alias = Master(full_name="Public B", is_active=True, booking_redirect_master_id=technical.id)
        session.add_all([public, alias])
        await session.flush()
        services = [BarberService(master_id=master.id, base_service_id=base.id, name=base.name,
                                  duration_minutes=duration, price=price, is_active=True)
                    for master, duration, price in [(public, 60, 500), (alias, 45, 650), (technical, 30, 800)]]
        session.add_all(services)
        session.add(MasterAvailabilityWindow(master_id=technical.id, start_at=start,
                                             end_at=start + timedelta(hours=4)))
        await session.commit()
    return public.id, alias.id, technical.id, [service.id for service in services], start


def selection(public_id, service_id, start):
    return BookingAlternativesRequest(master_id=public_id, service_ids=[service_id],
                                      desired_date=start.date() - timedelta(days=1), duration_minutes=60,
                                      page_size=100)


def create_payload(master_id, service_id, start, index=1, **extra):
    return PublicBookingCreate(master_id=master_id, service_ids=[service_id], start_at=start,
                               customer_name=f"Synthetic Customer {index}",
                               customer_phone=f"+38050123{index:04d}", **extra)


@pytest.mark.anyio
async def test_redirect_alternative_quote_create_agree_on_price_duration_and_persisted_identity(availability_database):
    _, sessions = availability_database
    public, _, physical, services, start = await catalog(sessions)
    async with sessions() as session:
        page = await BookingAlternativesService().find_page(session, selection(public, services[0], start))
        offer = next(item for item in page.same_master if item.start_at == start)
        assert offer.master.id == public and offer.service_ids == [services[0]]
        assert offer.duration_minutes == 60 and offer.price == 800
        quote = await quote_public_booking(PublicBookingQuoteRequest(
            master_id=public, service_ids=offer.service_ids, start_at=offer.start_at), session)
        assert quote.subtotal_amount == quote.total_amount == offer.price
        booking = await BookingServiceLayer().create_public_booking(session,
            create_payload(public, services[0], start, expected_total_amount=quote.total_amount),
            source=BookingSource.web)
        booking_id = booking.id
    async with sessions() as verify:
        rows = list((await verify.scalars(select(Booking))).all())
        assert len(rows) == 1 and rows[0].id == booking_id
        row = rows[0]
        await verify.refresh(row, attribute_names=["service_items"])
        assert row.master_id == physical and row.redirected_from_master_id == public
        assert row.service_id == services[2]
        assert [item.service_id for item in row.service_items] == [services[2]]
        assert row.start_at == start and row.end_at == start + timedelta(hours=1)
        assert row.subtotal_amount == row.total_amount == 800 and row.source == BookingSource.web


@pytest.mark.anyio
@pytest.mark.parametrize("state", list(WaitlistOfferStatus))
async def test_batched_alternatives_hold_status_and_expiry_are_live(availability_database, monkeypatch, state):
    _, sessions = availability_database
    public, _, physical, services, start = await catalog(sessions)
    now = datetime.now(UTC)
    async with sessions() as session:
        customer = Customer(phone="+380501239999", name="Synthetic Hold", is_active=True)
        session.add(customer)
        await session.flush()
        request = WaitlistRequest(customer_id=customer.id, preferred_master_id=public,
            desired_date=start.date(), duration_minutes=60, notification_consent=True,
            expires_at=start, cancel_token_hash="synthetic-cancel", dedup_key_hash="synthetic-dedup")
        session.add(request)
        await session.flush()
        session.add(WaitlistOffer(request_id=request.id, master_id=physical, start_at=start,
            end_at=start + timedelta(hours=1), token_hash="synthetic-hold", scheduled_at=now,
            expires_at=now + timedelta(minutes=1), status=state))
        await session.commit()
    active = state in {WaitlistOfferStatus.sent, WaitlistOfferStatus.delivered}
    async with sessions() as session:
        before = await BookingAlternativesService().find_page(session, selection(public, services[0], start))
        assert (start in {item.start_at for item in before.same_master}) is (not active)

    class AfterExpiry(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = now + timedelta(minutes=2)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(booking_module, "datetime", AfterExpiry)
    async with sessions() as session:
        after = await BookingAlternativesService().find_page(session, selection(public, services[0], start))
        assert start in {item.start_at for item in after.same_master}
        assert after.search_context_id == before.search_context_id
        assert after.total_count > before.total_count if active else after.total_count == before.total_count


@pytest.mark.anyio
@pytest.mark.parametrize("offset,expected", [(0, [201, 409]), (30, [201, 409]), (60, [201, 201])])
async def test_distinct_public_masters_share_physical_lock_for_overlapping_durations(availability_database, offset, expected):
    _, sessions = availability_database
    public, alias, physical, services, start = await catalog(sessions)
    ready = asyncio.Event()
    arrivals = 0

    async def attempt(master_id, service_id, instant, index):
        nonlocal arrivals
        async with sessions() as session:
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()
            try:
                await BookingServiceLayer().create_public_booking(
                    session, create_payload(master_id, service_id, instant, index))
                return 201
            except HTTPException as error:
                return error.status_code

    results = await asyncio.wait_for(asyncio.gather(
        attempt(public, services[0], start, 1),
        attempt(alias, services[1], start + timedelta(minutes=offset), 2)), timeout=20)
    assert sorted(results) == expected
    async with sessions() as verify:
        rows = list((await verify.scalars(select(Booking).order_by(Booking.start_at))).all())
        assert len(rows) == expected.count(201)
        assert all(row.master_id == physical for row in rows)
        assert all(left.end_at <= right.start_at for left, right in pairwise(rows))


@pytest.mark.anyio
async def test_failure_after_calendar_lock_rolls_back_customer_and_releases_lock(availability_database, monkeypatch):
    _, sessions = availability_database
    public, _, _, services, start = await catalog(sessions)
    service = BookingServiceLayer()

    async def fail_after_customer(*args, **kwargs):
        raise RuntimeError("Synthetic pricing failure after calendar lock")

    monkeypatch.setattr(service.promotion_service, "apply_to_booking", fail_after_customer)
    async with sessions() as session:
        with pytest.raises(RuntimeError, match="Synthetic pricing failure"):
            await service.create_public_booking(session, create_payload(public, services[0], start))
    async with sessions() as verify:
        assert await verify.scalar(select(func.count()).select_from(Booking)) == 0
        assert await verify.scalar(select(func.count()).select_from(Customer)) == 0
    async with sessions() as session:
        booking = await asyncio.wait_for(BookingServiceLayer().create_public_booking(
            session, create_payload(public, services[0], start, 2)), timeout=20)
        assert booking.start_at == start
    async with sessions() as verify:
        assert await verify.scalar(select(func.count()).select_from(Booking)) == 1
        assert await verify.scalar(select(func.count()).select_from(Customer)) == 1
