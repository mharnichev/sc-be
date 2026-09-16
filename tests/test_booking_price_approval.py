"""Atomic approved-price checks against isolated PostgreSQL schemas; no providers."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException, Response
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1.routes import bookings as routes
from app.models.booking import BarberService, Booking, BookingStatus, MasterAvailabilityWindow
from app.models.customer import Customer
from app.models.promotion import Promotion
from app.schemas.booking import PublicBookingCreate, PublicBookingQuoteRequest
from app.services.promotion import PromotionService
from tests.test_first_visit_integration import AT, PHONE, seed
from tests.test_segments_integration import database, anyio_backend  # noqa: F401


@pytest.fixture
def notifications(monkeypatch):
    calls = []
    for owner, name in [
        (routes.email_notification_service, "send_new_booking_to_master"),
        (routes.master_telegram_notification_service, "send_new_booking_to_master"),
        (routes.customer_activity_notification_service, "send_booking_confirmation"),
    ]:
        mock = AsyncMock()
        monkeypatch.setattr(owner, name, mock)
        calls.append(mock)
    monkeypatch.setattr(routes, "set_booking_browser_session", AsyncMock())
    return calls


async def prepare(database):
    pairs, promotion_id = await seed(database, customer=False)
    async with database() as session:
        session.add_all([MasterAvailabilityWindow(master_id=pair[0], start_at=AT,
                                                 end_at=AT + timedelta(hours=2)) for pair in pairs])
        await session.commit()
    return pairs, promotion_id


async def quote(database, pair):
    async with database() as session:
        return await routes.quote_public_booking(PublicBookingQuoteRequest(
            master_id=pair[0], service_id=pair[1], start_at=AT, customer_phone=PHONE), session)


async def submit(database, pair, **approval):
    background = BackgroundTasks()
    async with database() as session:
        try:
            result = await routes.create_public_booking(
                PublicBookingCreate(master_id=pair[0], service_id=pair[1], start_at=AT,
                                    customer_name="Price approval", customer_phone=PHONE, **approval),
                background, Response(), current_user=None, session=session, x_repeat_booking_token=None)
        except HTTPException:
            assert not background.tasks
            # Even an accidental later commit must not persist rejected customer/booking writes.
            await session.commit()
            raise
    await background()
    return result


@pytest.mark.anyio
async def test_concurrent_approved_quotes_only_save_one_booking_and_notify_once(database, notifications):
    pairs, _ = await prepare(database)
    quotes = [await quote(database, pair) for pair in pairs]
    assert [item.total_amount for item in quotes] == [800, 800]
    results = await asyncio.wait_for(asyncio.gather(*[
        submit(database, pair, expected_total_amount=item.total_amount)
        for pair, item in zip(pairs, quotes)
    ], return_exceptions=True), timeout=20)
    failures = [item for item in results if isinstance(item, HTTPException)]
    assert len(failures) == 1
    assert failures[0].status_code == 409
    assert failures[0].detail["code"] == "price_changed"
    assert failures[0].detail["total_amount"] == 1000
    successes = [item for item in results if not isinstance(item, Exception)]
    assert len(successes) == 1 and successes[0].total_amount == 800
    async with database() as session:
        saved = (await session.scalars(select(Booking))).all()
        assert len(saved) == 1
        assert saved[0].first_visit_customer_id == saved[0].customer_id
        assert len((await session.scalars(select(Customer))).all()) == 1
    assert [mock.await_count for mock in notifications] == [1, 1, 1]


@pytest.mark.anyio
@pytest.mark.parametrize("change,total", [("percent", 650), ("disabled", 1000), ("service_price", 960)])
async def test_quote_then_price_edit_rolls_back_customer_and_booking_without_notifications(
    database, notifications, change, total,
):
    pairs, promotion_id = await prepare(database)
    approved = await quote(database, pairs[0])
    async with database() as session:
        promotion = await session.get(Promotion, promotion_id)
        if change == "percent":
            promotion.discount_percent = 35
        elif change == "disabled":
            promotion.is_active = False
        else:
            service = await session.get(BarberService, pairs[0][1])
            service.price = 1200
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await submit(database, pairs[0], expected_total_amount=approved.total_amount)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "price_changed"
    assert error.value.detail["total_amount"] == total
    async with database() as session:
        assert not (await session.scalars(select(Booking))).all()
        assert not (await session.scalars(select(Customer))).all()
    assert [mock.await_count for mock in notifications] == [0, 0, 0]
    refreshed = await quote(database, pairs[0])
    saved = await submit(database, pairs[0], expected_total_amount=refreshed.total_amount)
    assert saved.total_amount == total
    assert [mock.await_count for mock in notifications] == [1, 1, 1]


@pytest.mark.anyio
@pytest.mark.parametrize("expected", [None, 0])
async def test_matching_zero_and_legacy_omission(database, notifications, expected):
    pairs, promotion_id = await prepare(database)
    async with database() as session:
        promotion = await session.get(Promotion, promotion_id)
        promotion.discount_percent = 100
        await session.commit()
    approval = {} if expected is None else {"expected_total_amount": expected}
    result = await submit(database, pairs[0], **approval)
    assert result.total_amount == 0
    assert [mock.await_count for mock in notifications] == [1, 1, 1]


@pytest.mark.parametrize("invalid", [-1, 1.5, "800", True])
def test_expected_total_requires_nonnegative_integer(invalid):
    with pytest.raises(ValidationError):
        PublicBookingCreate(master_id=1, service_id=1, start_at=AT, customer_name="Price approval",
                            customer_phone=PHONE, expected_total_amount=invalid)


@pytest.mark.anyio
async def test_alias_enrichment_and_completion_acquire_customer_locks_in_same_order(
    database, notifications, monkeypatch,
):
    pairs, _ = await prepare(database)
    async with database() as session:
        older = Customer(phone="+0501234567", name="Older alias")
        session.add(older)
        await session.flush()
        selected = Customer(phone=PHONE, name="")
        session.add(selected)
        await session.flush()
        historic = Booking(master_id=pairs[1][0], service_id=pairs[1][1], customer_id=older.id,
                           customer_name="Older alias", customer_phone=older.phone,
                           start_at=AT-timedelta(days=1), end_at=AT-timedelta(days=1)+timedelta(minutes=30),
                           status=BookingStatus.confirmed)
        session.add(historic)
        await session.commit()
        older_id, selected_id, historic_id = older.id, selected.id, historic.id

    low_locked = asyncio.Event()
    creator_locking = asyncio.Event()
    original_lock = PromotionService.lock_customer

    async def observe_lock(self, session, customer):
        if customer.id == selected_id:
            creator_locking.set()
        return await original_lock(self, session, customer)

    monkeypatch.setattr(PromotionService, "lock_customer", observe_lock)

    async def complete():
        async with database() as session:
            await session.execute(select(Customer).where(Customer.id == older_id).with_for_update())
            low_locked.set()
            await creator_locking.wait()
            booking = await session.get(Booking, historic_id)
            await PromotionService().sync_first_visit_status(session, booking, BookingStatus.completed)
            booking.status = BookingStatus.completed
            await session.commit()

    async def book():
        await low_locked.wait()
        return await submit(database, pairs[0], expected_total_amount=800)

    # Old enrichment flushed the higher-ID row before requesting the lower one,
    # creating a deadlock with this completion's lower-then-higher row locks.
    results = await asyncio.wait_for(asyncio.gather(complete(), book(), return_exceptions=True), timeout=10)
    assert results[0] is None
    assert isinstance(results[1], HTTPException)
    assert results[1].detail["code"] == "price_changed"
    async with database() as session:
        assert len((await session.scalars(select(Booking))).all()) == 1
        assert (await session.get(Customer, selected_id)).name == ""
    assert [mock.await_count for mock in notifications] == [0, 0, 0]
