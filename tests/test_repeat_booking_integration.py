"""Public repeat-booking contracts against opt-in, disposable PostgreSQL schemas."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.api.v1.routes import bookings as routes
from app.api.v1.routes import repeat_booking as repeat_routes
from app.core.database import get_db_session
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master, MasterAvailabilityWindow
from app.models.customer import Customer
from app.models.repeat_booking import RepeatBookingEvent, RepeatBookingEventType, RepeatBookingOffer, RepeatBookingOfferStatus
from app.services.repeat_booking import repeat_booking_service
from tests.test_segments_integration import anyio_backend, database  # noqa: F401

PHONE = "+380500000001"
TOKEN = "r" * 43


@pytest.fixture
async def scenario(database):
    kyiv = ZoneInfo("Europe/Kyiv")
    day = datetime.now(kyiv).date() + timedelta(days=1)
    while day.weekday() == 0:
        day += timedelta(days=1)
    at = datetime(day.year, day.month, day.day, 12, tzinfo=kyiv)
    now = datetime.now(UTC)
    async with database() as session:
        master = Master(full_name="Repeat integration master")
        customer = Customer(phone=PHONE, name="Repeat integration customer")
        session.add_all([master, customer])
        await session.flush()
        services = [
            BarberService(master_id=master.id, name="Haircut", duration_minutes=30, price=1000),
            BarberService(master_id=master.id, name="Beard", duration_minutes=15, price=500),
        ]
        session.add_all(services)
        await session.flush()
        source = Booking(
            master_id=master.id, service_id=services[0].id, customer_id=customer.id,
            customer_name=customer.name, customer_phone=PHONE,
            start_at=now - timedelta(days=30, hours=1), end_at=now - timedelta(days=30),
            completed_at=now - timedelta(days=30), status=BookingStatus.completed,
        )
        session.add(source)
        await session.flush()
        session.add_all([
            *(BookingServiceItem(booking_id=source.id, service_id=service.id, position=i, price_amount=service.price)
              for i, service in enumerate(services)),
            MasterAvailabilityWindow(master_id=master.id, start_at=at, end_at=at + timedelta(hours=6)),
        ])
        offer = RepeatBookingOffer(
            completed_booking_id=source.id, customer_id=customer.id,
            preferred_master_id=master.id, service_ids=[service.id for service in services],
            token_hash=repeat_booking_service.hash_token(TOKEN), status=RepeatBookingOfferStatus.sent,
            scheduled_at=now - timedelta(minutes=1), sent_at=now,
            expires_at=now + timedelta(days=7),
        )
        session.add(offer)
        await session.commit()
        return {
            "at": at, "now": now, "source_id": source.id, "customer_id": customer.id,
            "master_id": master.id, "service_ids": [service.id for service in services],
            "offer_id": offer.id,
        }


@pytest.fixture
async def client(database, monkeypatch):
    notifications = []
    for owner in [routes.email_notification_service, routes.master_telegram_notification_service,
                  routes.customer_activity_notification_service]:
        name = "send_booking_confirmation" if owner is routes.customer_activity_notification_service else "send_new_booking_to_master"
        mock = AsyncMock()
        monkeypatch.setattr(owner, name, mock)
        notifications.append(mock)
    monkeypatch.setattr(routes, "set_booking_browser_session", AsyncMock())
    app = FastAPI()
    app.include_router(routes.public_router)
    app.include_router(repeat_routes.public_router)

    async def session_dependency():
        async with database() as session:
            try:
                yield session
            finally:
                # A later commit must not persist writes rejected by booking creation.
                await session.commit()

    async def anonymous_user():
        return None

    app.dependency_overrides[get_db_session] = session_dependency
    app.dependency_overrides[routes.get_optional_admin_user] = anonymous_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://repeat.test") as http:
        yield http, notifications


def payload(scenario, *, hours=0, **changes):
    return {
        "master_id": scenario["master_id"], "service_ids": scenario["service_ids"],
        "customer_name": "Repeat integration customer", "customer_phone": PHONE,
        "start_at": (scenario["at"] + timedelta(hours=hours)).isoformat(),
        "expected_total_amount": 1500, **changes,
    }


async def submit(client, scenario, *, hours=0, **changes):
    return await client[0].post("/bookings", json=payload(scenario, hours=hours, **changes),
                                headers={"X-Repeat-Booking-Token": TOKEN})


async def add_other_visit(database, scenario, visit_status):
    at = scenario["at"] + timedelta(hours=4)
    completed_at = None
    if visit_status == BookingStatus.completed:
        at = scenario["now"] - timedelta(days=1, hours=1)
        completed_at = at + timedelta(hours=1)
    async with database() as session:
        booking = Booking(
            master_id=scenario["master_id"], service_id=scenario["service_ids"][0],
            customer_id=scenario["customer_id"], customer_name="Repeat integration customer",
            customer_phone=PHONE, start_at=at, end_at=at + timedelta(hours=1),
            status=visit_status, completed_at=completed_at,
        )
        session.add(booking)
        await session.commit()
        return booking.id


@pytest.mark.anyio
async def test_public_repeat_booking_consumes_offer_atomically_and_rejects_reuse(database, scenario, client):
    for method, path in [("GET", "/repeat-booking/context"), ("POST", "/repeat-booking/start")]:
        response = await client[0].request(method, path, headers={"X-Repeat-Booking-Token": TOKEN})
        assert response.status_code == 200
    response = await submit(client, scenario)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["total_amount"] == 1500
    async with database() as session:
        bookings = (await session.scalars(select(Booking).order_by(Booking.id))).all()
        assert len(bookings) == 2
        created = next(b for b in bookings if b.id != scenario["source_id"])
        assert created.status == BookingStatus.confirmed
        assert created.customer_id == scenario["customer_id"]
        assert created.end_at - created.start_at == timedelta(minutes=45)
        assert created.first_visit_customer_id is None
        assert created.promotion_id is None
        items = (await session.scalars(select(BookingServiceItem).where(BookingServiceItem.booking_id == created.id).order_by(BookingServiceItem.position))).all()
        assert [item.service_id for item in items] == scenario["service_ids"]
        offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
        assert offer.status == RepeatBookingOfferStatus.booked
        assert offer.result_booking_id == created.id
        assert offer.token_hash is None and offer.revoked_at is not None
        event_types = (await session.scalars(select(RepeatBookingEvent.event_type))).all()
        assert sorted(event_types) == sorted([RepeatBookingEventType.link_opened.value, RepeatBookingEventType.booking_started.value])
    assert [mock.await_count for mock in client[1]] == [1, 1, 1]
    reused = await submit(client, scenario, hours=2)
    assert reused.status_code == 401
    async with database() as session:
        assert len((await session.scalars(select(Booking))).all()) == 2
    assert [mock.await_count for mock in client[1]] == [1, 1, 1]


@pytest.mark.anyio
@pytest.mark.parametrize("reason,expected_status", [
    ("expired", 401), ("revoked", 401), ("used", 401),
    ("wrong_owner", 403), ("wrong_selection", 409),
    ("pending_visit", 401), ("confirmed_visit", 401), ("completed_visit", 401),
    ("price_changed", 409),
])
async def test_public_repeat_booking_rejection_rolls_back_every_write(database, scenario, client, reason, expected_status):
    changes = {}
    if reason in {"pending_visit", "confirmed_visit", "completed_visit"}:
        await add_other_visit(database, scenario, BookingStatus(reason.removesuffix("_visit")))
    elif reason == "wrong_owner":
        changes["customer_phone"] = "+380500000002"
    elif reason == "wrong_selection":
        changes.update(service_ids=scenario["service_ids"][:1], expected_total_amount=1000)
    elif reason == "price_changed":
        changes["expected_total_amount"] = 1400
    else:
        async with database() as session:
            offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
            if reason == "expired":
                offer.expires_at = scenario["now"] - timedelta(seconds=1)
            elif reason == "revoked":
                offer.revoked_at = scenario["now"]
            else:
                offer.status = RepeatBookingOfferStatus.booked
                offer.token_hash = None
            await session.commit()
    async with database() as session:
        original_ids = list(await session.scalars(select(Booking.id).order_by(Booking.id)))
        original_offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
        original_state = (original_offer.status, original_offer.token_hash, original_offer.revoked_at)
    response = await submit(client, scenario, **changes)
    assert response.status_code == expected_status, response.text
    if reason == "price_changed":
        assert response.json()["detail"]["code"] == "price_changed"
        assert response.json()["detail"]["total_amount"] == 1500
    async with database() as session:
        assert list(await session.scalars(select(Booking.id).order_by(Booking.id))) == original_ids
        assert len((await session.scalars(select(Customer))).all()) == 1
        offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
        assert (offer.status, offer.token_hash, offer.revoked_at) == original_state
        assert offer.result_booking_id is None
        assert not list(await session.scalars(select(RepeatBookingEvent)))
    assert [mock.await_count for mock in client[1]] == [0, 0, 0]


@pytest.mark.anyio
@pytest.mark.parametrize("method,path", [("GET", "/repeat-booking/context"), ("POST", "/repeat-booking/start")])
@pytest.mark.parametrize("visit_status", [BookingStatus.pending, BookingStatus.confirmed])
async def test_context_and_start_still_reject_other_active_visits(database, scenario, client, method, path, visit_status):
    await add_other_visit(database, scenario, visit_status)
    response = await client[0].request(method, path, headers={"X-Repeat-Booking-Token": TOKEN})
    assert response.status_code == 401
    async with database() as session:
        offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
        assert offer.status == RepeatBookingOfferStatus.skipped
        assert offer.skip_reason == "future_booking_exists"
        assert offer.token_hash is None
        assert offer.result_booking_id is None
    assert [mock.await_count for mock in client[1]] == [0, 0, 0]


@pytest.mark.anyio
async def test_concurrent_public_repeat_submissions_have_one_winner(database, scenario, client, monkeypatch):
    barrier = asyncio.Barrier(2)
    backend_ids = set()
    original_resolve = routes.service.resolve_booking_master

    async def resolve_together(session, *args, **kwargs):
        backend_ids.add(await session.scalar(text("SELECT pg_backend_pid()")))
        await barrier.wait()
        return await original_resolve(session, *args, **kwargs)

    monkeypatch.setattr(routes.service, "resolve_booking_master", resolve_together)
    responses = await asyncio.wait_for(asyncio.gather(
        submit(client, scenario), submit(client, scenario, hours=2)), timeout=20)
    assert len(backend_ids) == 2
    assert sorted(response.status_code for response in responses) == [201, 401]
    async with database() as session:
        bookings = (await session.scalars(select(Booking))).all()
        assert len(bookings) == 2
        offer = await session.get(RepeatBookingOffer, scenario["offer_id"])
        assert offer.status == RepeatBookingOfferStatus.booked
        assert offer.result_booking_id in {b.id for b in bookings if b.id != scenario["source_id"]}
        assert offer.token_hash is None
        events = list(await session.scalars(select(RepeatBookingEvent.event_type)))
        assert sorted(events) == sorted([RepeatBookingEventType.link_opened.value, RepeatBookingEventType.booking_started.value])
    assert [mock.await_count for mock in client[1]] == [1, 1, 1]
