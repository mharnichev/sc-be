"""Isolated local PostgreSQL only; all message providers record calls in memory."""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_segments_integration import database, anyio_backend, add_booking, add_customer, SandboxProvider  # noqa: F401
from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignStatus, CampaignType, ClientCommunicationPreference, ConsentStatus,
    MessageChannel, MessagePurpose, MessageRecipient, MessageTemplate, ReviewRequest,
)
from app.services.booking import KYIV_TZ
from app.services.booking_completion import complete_closed_workday_bookings
from app.services import messaging
from app.services.messaging import MessagingService
from app.models.repeat_booking import RepeatBookingOffer, RepeatBookingOfferStatus
from app.services.repeat_booking import RepeatBookingService

pytestmark = pytest.mark.anyio
AT = datetime(2026, 10, 11, 20, tzinfo=KYIV_TZ)


@pytest.fixture(autouse=True)
def enable_completion(monkeypatch):
    monkeypatch.setattr(settings, "booking_auto_completion_enabled", True)
    monkeypatch.setattr(settings, "booking_auto_completion_starts_on", date(2026, 10, 11))


async def seed_visit(database):
    async with database() as session:
        customer = await add_customer(session)
        booking = await add_booking(session, customer, AT, status=BookingStatus.confirmed)
        await session.commit()
        return booking.id, customer.id


async def test_concurrent_workers_complete_once_with_real_customer_state(database):
    booking_id, customer_id = await seed_visit(database)

    async def run():
        async with database() as session:
            return await complete_closed_workday_bookings(session, now=AT)

    counts = await asyncio.wait_for(asyncio.gather(run(), run()), timeout=10)
    assert sum(counts) == 1
    async with database() as session:
        visit = await session.get(Booking, booking_id)
        assert visit.status == BookingStatus.completed
        assert visit.completed_at == AT
        customer = await session.get(Customer, customer_id)
        assert customer.first_visit_completed_at == AT


async def test_locked_manual_cancellation_is_skipped_and_never_completed(database):
    booking_id, _ = await seed_visit(database)
    async with database() as manual:
        visit = (await manual.execute(select(Booking).where(Booking.id == booking_id).with_for_update())).scalar_one()
        async with database() as worker:
            assert await asyncio.wait_for(complete_closed_workday_bookings(worker, now=AT), timeout=5) == 0
        visit.status = BookingStatus.cancelled
        await manual.commit()
    async with database() as worker:
        assert await complete_closed_workday_bookings(worker, now=AT) == 0
        assert (await worker.get(Booking, booking_id)).status == BookingStatus.cancelled


async def test_evening_completion_sends_review_only_next_morning(database, monkeypatch):
    booking_id, customer_id = await seed_visit(database)

    class Clock(datetime):
        instant = AT

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

    monkeypatch.setattr(messaging, "datetime", Clock)
    sms = SandboxProvider()
    service = MessagingService(providers={MessageChannel.sms: sms})
    async with database() as session:
        template = MessageTemplate(name="Synthetic review", channel=MessageChannel.sms, body="Review {{review_link}}")
        session.add(template)
        await session.flush()
        session.add(Campaign(name="Synthetic reviews", type=CampaignType.post_visit_review_request,
            status=CampaignStatus.active, channel=MessageChannel.sms, purpose=MessagePurpose.review_request,
            template_id=template.id, metadata_json={"primary_channel": "sms", "fallback_channel": None,
                "send_time": "10:00", "quiet_hours_enabled": False}))
        session.add(ClientCommunicationPreference(customer_id=customer_id, marketing_consent=ConsentStatus.opted_in))
        await session.commit()
        assert await complete_closed_workday_bookings(session, now=AT) == 1
        assert await service.create_review_requests_for_completed_appointments(session) == 1
        request = (await session.execute(select(ReviewRequest).where(ReviewRequest.appointment_id == booking_id))).scalar_one()
        assert request.scheduled_at == AT + timedelta(hours=14)
        assert await service.process_pending_review_requests(session) == 0
        assert sms.sent == []
        Clock.instant = AT + timedelta(hours=14)
        assert await service.process_pending_review_requests(session) == 1
        assert len(sms.sent) == 1
        recipient = (await session.execute(select(MessageRecipient).where(MessageRecipient.appointment_id == booking_id))).scalar_one()
        assert recipient.sent_at == Clock.instant


async def test_recovered_visits_preserve_order_for_repeat_offers(database, monkeypatch):
    from app.services.repeat_booking import repeat_booking_service
    class RecoveryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = AT + timedelta(days=2)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(messaging, "datetime", RecoveryClock)
    async with database() as session:
        customer = await add_customer(session)
        older = await add_booking(session, customer, AT - timedelta(hours=3), status=BookingStatus.confirmed)
        newer = await add_booking(session, customer, AT, status=BookingStatus.confirmed)
        await session.commit()
        assert await complete_closed_workday_bookings(session, now=AT + timedelta(days=2)) == 2
        await session.refresh(older)
        await session.refresh(newer)
        assert older.completed_at < newer.completed_at
        assert older.completed_at > AT + timedelta(days=1)
        assert newer.completed_at == AT + timedelta(days=2)
        assert await repeat_booking_service._has_newer_completion(session, customer_id=customer.id,
            source_booking_id=older.id, start_at=older.start_at)
        assert not await repeat_booking_service._has_newer_completion(session, customer_id=customer.id,
            source_booking_id=newer.id, start_at=newer.start_at)

        template = MessageTemplate(name="Recovered review", channel=MessageChannel.sms, body="Review {{review_link}}")
        session.add(template)
        await session.flush()
        session.add(Campaign(name="Recovered reviews", type=CampaignType.post_visit_review_request,
            status=CampaignStatus.active, channel=MessageChannel.sms, purpose=MessagePurpose.review_request,
            template_id=template.id, metadata_json={"primary_channel": "sms", "send_time": "10:00"}))
        session.add(ClientCommunicationPreference(customer_id=customer.id, marketing_consent=ConsentStatus.opted_in))
        await session.commit()
        service = MessagingService(providers={MessageChannel.sms: SandboxProvider()})
        assert await service.create_review_requests_for_completed_appointments(session) > 0
        requests = (await session.execute(select(ReviewRequest))).scalars().all()
        assert requests
        assert all(request.scheduled_at == AT + timedelta(days=2, hours=14) for request in requests)


@pytest.mark.parametrize("same_start", [False, True])
async def test_older_locked_visit_retry_preserves_newer_offer_and_eligibility(database, same_start):
    retry_at = AT + timedelta(minutes=1)
    service = RepeatBookingService(now=lambda: retry_at)
    token = "n" * 43
    async with database() as session:
        customer = await add_customer(session)
        older = await add_booking(session, customer, AT if same_start else AT - timedelta(hours=3),
                                  status=BookingStatus.confirmed)
        newer = await add_booking(session, customer, AT, status=BookingStatus.confirmed)
        session.add(ClientCommunicationPreference(customer_id=customer.id,
            marketing_consent=ConsentStatus.opted_in, telegram_chat_id="sandbox-chat"))
        await session.commit()
        older_id, newer_id = older.id, newer.id
        assert older_id < newer_id

    async with database() as locked:
        await locked.execute(select(Booking).where(Booking.id == older_id).with_for_update())
        async with database() as worker:
            assert await asyncio.wait_for(complete_closed_workday_bookings(worker, now=AT), timeout=5) == 1
        async with database() as session:
            newer = (await session.execute(select(Booking).where(Booking.id == newer_id)
                .options(*service._booking_load_options()))).scalar_one()
            assert newer.completed_at == AT
            offer = await service.schedule_booking(session, newer, at=AT)
            assert offer.status == RepeatBookingOfferStatus.scheduled
            assert offer.scheduled_at == service.scheduled_time(newer.completed_at, list(offer.service_ids))
            scheduled_at = offer.scheduled_at
            offer.status = RepeatBookingOfferStatus.sent
            offer.token_hash = service.hash_token(token)
            offer.expires_at = AT + timedelta(days=7)
            await session.commit()
            offer_id = offer.id
        await locked.rollback()

    async with database() as worker:
        assert await complete_closed_workday_bookings(worker, now=retry_at) == 1
    async with database() as session:
        visits = {booking.id: booking for booking in (await session.scalars(
            select(Booking).where(Booking.id.in_([older_id, newer_id]))
            .options(*service._booking_load_options())
        )).all()}
        older, newer = visits[older_id], visits[newer_id]
        assert older.completed_at == retry_at > newer.completed_at
        assert await service.eligibility_reason(session, older, service_ids=older.service_ids,
            at=retry_at, check_frequency=False) == "newer_completed_booking"
        assert await service.eligibility_reason(session, newer, service_ids=newer.service_ids,
            at=retry_at, check_frequency=False) is None
        offer = await session.get(RepeatBookingOffer, offer_id)
        assert offer.status == RepeatBookingOfferStatus.sent
        assert offer.token_hash == service.hash_token(token)
        assert offer.revoked_at is None
        assert offer.scheduled_at == scheduled_at
        assert (await service._valid_offer(session, token)).id == offer_id
        assert await service.revoke_superseded_offers(session, older) == 0

        older_offer = await service.schedule_booking(session, older, at=retry_at)
        assert older_offer.status == RepeatBookingOfferStatus.skipped
        assert older_offer.skip_reason == "newer_completed_booking"
        older_token = "o" * 43
        older_offer.status = RepeatBookingOfferStatus.sent
        older_offer.token_hash = service.hash_token(older_token)
        older_offer.revoked_at = None
        older_offer.expires_at = AT + timedelta(days=7)
        with pytest.raises(HTTPException) as invalid:
            await service._valid_offer(session, older_token)
        assert invalid.value.status_code == 401
        assert older_offer.skip_reason == "newer_completed_booking"
        # A genuinely newer visit must still revoke an older open offer.
        older_offer.status = RepeatBookingOfferStatus.sent
        older_offer.token_hash = service.hash_token(older_token)
        older_offer.revoked_at = None
        assert await service.revoke_superseded_offers(session, newer) == 1
        assert older_offer.status == RepeatBookingOfferStatus.skipped
        assert older_offer.token_hash is None
        assert offer.status == RepeatBookingOfferStatus.sent
