"""Telegram ownership, interruption, and recovery against isolated PostgreSQL."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text

from test_inventory_integration import anyio_backend, database  # noqa: F401
from app.models.customer import Customer
from app.models.booking import BarberService, Booking, Master
from app.models.messaging import (Campaign, CampaignStatus, CampaignType, ClientCommunicationPreference,
    MasterMessageDelivery, MasterScheduleReminder, MessageChannel, MessageDeliveryStatus,
    MessagePurpose, MessageRecipient, ReviewPlatform, ReviewRequest, ReviewRequestStatus, TelegramDeliveryAttempt)
from app.services.messaging import MessagingService, ProviderSendResult
from app.services.telegram_delivery import (TelegramDeliveryUncertain,
    TelegramDestinationUnavailable, TelegramRejected, durable_telegram_send)

pytestmark = pytest.mark.anyio


class Provider:
    def __init__(self, *, pause=False, failure=None):
        self.calls = 0
        self.pause = pause
        self.failure = failure
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, **kwargs):
        self.calls += 1
        self.entered.set()
        if self.pause:
            await self.release.wait()
        if self.failure:
            raise self.failure
        return ProviderSendResult("synthetic-accepted", {"ok": True})


async def send(database, provider, key="synthetic-message"):
    async with database() as session:
        return await durable_telegram_send(session, provider, key=key,
            destination="synthetic-chat", body="Synthetic message")


async def state(database, key="synthetic-message"):
    async with database() as session:
        row = await session.get(TelegramDeliveryAttempt, key)
        return row.state, row.attempts, row.provider_message_id


async def test_provider_ownership_is_visible_before_acceptance_and_concurrent_send_is_suppressed(database):
    provider = Provider(pause=True)
    first = asyncio.create_task(send(database, provider))
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        assert await state(database) == ("attempting", 1, None)
        with pytest.raises(TelegramDeliveryUncertain):
            await asyncio.wait_for(send(database, provider), 5)
        assert provider.calls == 1
        provider.release.set()
        assert (await asyncio.wait_for(first, 5)).provider_message_id == "synthetic-accepted"
        assert await state(database) == ("accepted", 1, "synthetic-accepted")
        assert (await send(database, provider)).provider_message_id == "synthetic-accepted"
        assert provider.calls == 1
    finally:
        provider.release.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_cancellation_after_possible_acceptance_does_not_resend_on_restart(database):
    provider = Provider(pause=True)
    first = asyncio.create_task(send(database, provider))
    await asyncio.wait_for(provider.entered.wait(), 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await state(database) == ("attempting", 1, None)
    with pytest.raises(TelegramDeliveryUncertain):
        await send(database, provider)
    assert provider.calls == 1


async def test_timeout_is_ambiguous_and_never_automatically_retried(database):
    provider = Provider(failure=TimeoutError("synthetic timeout"))
    with pytest.raises(TelegramDeliveryUncertain):
        await send(database, provider)
    assert await state(database) == ("ambiguous", 1, None)
    provider.failure = None
    with pytest.raises(TelegramDeliveryUncertain):
        await send(database, provider)
    assert provider.calls == 1


async def test_definitive_rejection_allows_safe_retry_and_records_separate_attempt(database):
    provider = Provider(failure=TelegramRejected("synthetic structured rejection"))
    with pytest.raises(TelegramRejected):
        await send(database, provider)
    assert await state(database) == ("rejected", 1, None)
    provider.failure = None
    await send(database, provider)
    assert await state(database) == ("accepted", 2, "synthetic-accepted")
    assert provider.calls == 2


async def seed_review(database):
    recipient_id = await seed_recipient(database)
    start = datetime.now(UTC) - timedelta(days=2)
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        campaign = await session.get(Campaign, recipient.campaign_id)
        campaign.type = CampaignType.post_visit_review_request
        campaign.purpose = MessagePurpose.review_request
        campaign.metadata_json = {"message_body": "Synthetic review {review_link}"}
        master = Master(full_name="Synthetic review master")
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Synthetic", duration_minutes=30, price=100)
        session.add(service)
        await session.flush()
        booking = Booking(master_id=master.id, service_id=service.id, customer_id=recipient.customer_id,
            customer_name="Synthetic", customer_phone="+380501112297", start_at=start,
            end_at=start + timedelta(minutes=30))
        session.add(booking)
        await session.flush()
        recipient.appointment_id = booking.id
        request = ReviewRequest(campaign_id=campaign.id, appointment_id=booking.id, customer_id=recipient.customer_id,
            master_id=master.id, recipient_id=recipient.id, platform=ReviewPlatform.internal,
            review_url="https://example.test/review", channel=MessageChannel.telegram)
        session.add(request)
        await session.commit()
        return recipient_id, request.id


async def test_rejected_review_admin_retry_preserves_frozen_link_and_matching_token(database, monkeypatch):
    from app.core.config import settings
    from app.services.master_reviews import review_token_hash
    monkeypatch.setattr(settings, "messaging_max_retry_attempts", 1)
    recipient_id, request_id = await seed_review(database)
    provider = Provider(failure=TelegramRejected("synthetic definitive rejection"))
    service = MessagingService()
    service.providers[MessageChannel.telegram] = provider
    async with database() as session:
        assert await service.process_pending_messages(session) == 1
        recipient = await session.get(MessageRecipient, recipient_id)
        assert recipient.status == MessageDeliveryStatus.failed
        body = recipient.rendered_message
        token = body.rsplit("#", 1)[1]
        assert (await session.get(ReviewRequest, request_id)).token_hash == review_token_hash(token)
        assert await service.retry_failed(session) == 1
        assert (await session.get(ReviewRequest, request_id)).token_hash == review_token_hash(token)
        provider.failure = None
        assert await service.process_pending_messages(session) == 1
        recipient = await session.get(MessageRecipient, recipient_id)
        review = await session.get(ReviewRequest, request_id)
        assert recipient.rendered_message == body and recipient.status == MessageDeliveryStatus.sent
        assert review.token_hash == review_token_hash(token) and review.status == ReviewRequestStatus.sent
        assert review.expires_at > datetime.now(UTC)
    assert provider.calls == 2


@pytest.mark.parametrize("recovery", ["scheduler", "direct"])
async def test_review_accepted_projection_survives_later_dnc_and_pause(database, monkeypatch, recovery):
    from app.services.messaging import _recipient_delivery_load_options
    recipient_id, request_id = await seed_review(database)
    provider = Provider()
    service = MessagingService()
    service.providers[MessageChannel.telegram] = provider
    original = service._log_from_recipient
    def interrupted(recipient, status, **kwargs):
        if status == MessageDeliveryStatus.sent:
            raise asyncio.CancelledError("synthetic projection interruption")
        return original(recipient, status, **kwargs)
    monkeypatch.setattr(service, "_log_from_recipient", interrupted)
    async with database() as session:
        with pytest.raises(asyncio.CancelledError):
            await service.process_pending_messages(session)
    monkeypatch.setattr(service, "_log_from_recipient", original)
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        campaign = await session.get(Campaign, recipient.campaign_id)
        campaign.status = CampaignStatus.paused
        preference = await service.get_preference(session, recipient.customer_id)
        preference.do_not_contact = True
        await session.commit()
    async with database() as session:
        if recovery == "scheduler":
            assert await service.process_pending_messages(session) == 0
        else:
            recipient = await session.scalar(select(MessageRecipient).where(MessageRecipient.id == recipient_id)
                .options(*_recipient_delivery_load_options()))
            await service.send_recipient(session, recipient)
        recipient = await session.get(MessageRecipient, recipient_id)
        review = await session.get(ReviewRequest, request_id)
        assert recipient.status == MessageDeliveryStatus.sent and recipient.provider_message_id == "synthetic-accepted"
        assert review.status == ReviewRequestStatus.sent and review.sent_at == recipient.sent_at
    assert provider.calls == 1


async def test_paused_schedule_still_projects_known_accepted_outcome_without_new_send(database):
    from app.services.master_schedule_reminders import MasterScheduleReminderService
    async with database() as session:
        master = Master(full_name="Synthetic", telegram_chat_id="synthetic-chat")
        campaign = Campaign(name="Synthetic paused", type=CampaignType.master_schedule_reminder,
            status=CampaignStatus.paused, channel=MessageChannel.telegram, purpose=MessagePurpose.transactional)
        session.add_all([master, campaign])
        await session.flush()
        record = MasterScheduleReminder(campaign_id=campaign.id, master_id=master.id,
            target_month=date(2026, 9, 1), initial_attempts=1)
        session.add(record)
        session.add(TelegramDeliveryAttempt(key=f"master-schedule:{campaign.id}:{master.id}:2026-09-01:initial",
            state="accepted", destination="synthetic-chat", body="Synthetic", attempts=1,
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC), provider_message_id="synthetic-accepted"))
        await session.commit()
        record_id = record.id
    provider = Provider()
    async with database() as session:
        assert await MasterScheduleReminderService(provider).process_due(session) == 0
        record = await session.get(MasterScheduleReminder, record_id)
        assert record.initial_sent_at is not None and record.initial_provider_message_id == "synthetic-accepted"
    assert provider.calls == 0


async def test_paused_master_lifecycle_projects_known_acceptance_without_sending_new_work(database, monkeypatch):
    from app.services import master_notifications
    from app.services.master_notifications import MasterCampaignNotificationService, NewBookingTelegram
    monkeypatch.setattr(master_notifications, "AsyncSessionLocal", database)
    start = datetime.now(UTC)
    async with database() as session:
        master = Master(full_name="Synthetic", telegram_chat_id="synthetic-chat")
        campaign = Campaign(name="Synthetic paused", type=CampaignType.master_booking_created,
            status=CampaignStatus.paused, channel=MessageChannel.telegram, purpose=MessagePurpose.transactional)
        session.add_all([master, campaign])
        await session.flush()
        delivery = MasterMessageDelivery(campaign_id=campaign.id, master_id=master.id, trigger="booking_created",
            channel=MessageChannel.telegram, status=MessageDeliveryStatus.pending,
            idempotency_key="master:booking_created:booking:42", attempts=1)
        session.add(delivery)
        session.add(TelegramDeliveryAttempt(key=delivery.idempotency_key, state="accepted",
            destination="synthetic-chat", body="Synthetic", attempts=1, started_at=start,
            completed_at=start, provider_message_id="synthetic-accepted"))
        await session.commit()
        delivery_id = delivery.id
        notification = NewBookingTelegram(booking_id=42, master_id=master.id, master_name="Synthetic",
            telegram_chat_id="synthetic-chat", service_name="Synthetic", customer_name="Synthetic",
            customer_phone="+380501112291", customer_comment=None, start_at=start,
            end_at=start + timedelta(minutes=30))
    provider = Provider()
    service = MasterCampaignNotificationService(provider)
    await service.send_new_booking_to_master(notification)
    await service.send_new_booking_to_master(replace(notification, booking_id=43))
    async with database() as session:
        delivery = await session.get(MasterMessageDelivery, delivery_id)
        assert delivery.status == MessageDeliveryStatus.sent and delivery.provider_message_id == "synthetic-accepted"
    assert provider.calls == 0


@pytest.mark.parametrize("definitive", [True, False])
async def test_review_sms_fallback_requires_definitive_destination_rejection(database, monkeypatch, definitive):
    from app.core.config import settings
    monkeypatch.setattr(settings, "messaging_max_retry_attempts", 1)
    recipient_id = await seed_recipient(database)
    start = datetime.now(UTC) - timedelta(days=2)
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        campaign = await session.get(Campaign, recipient.campaign_id)
        campaign.purpose = MessagePurpose.review_request
        campaign.type = CampaignType.post_visit_review_request
        campaign.metadata_json = {"message_body": "Synthetic review {review_link}"}
        master = Master(full_name="Synthetic review master")
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Synthetic", duration_minutes=30, price=100)
        session.add(service)
        await session.flush()
        booking = Booking(master_id=master.id, service_id=service.id, customer_id=recipient.customer_id,
            customer_name="Synthetic", customer_phone="+380501112297", start_at=start,
            end_at=start + timedelta(minutes=30))
        session.add(booking)
        await session.flush()
        recipient.appointment_id = booking.id
        session.add(ReviewRequest(campaign_id=campaign.id, appointment_id=booking.id,
            customer_id=recipient.customer_id, master_id=master.id, recipient_id=recipient.id,
            platform=ReviewPlatform.internal, review_url="https://example.test/review",
            channel=MessageChannel.telegram, fallback_channel=MessageChannel.sms))
        await session.commit()
    telegram = Provider(failure=TelegramDestinationUnavailable("synthetic blocked destination") if definitive else TimeoutError("synthetic unknown"))
    sms = Provider()
    service = MessagingService()
    service.providers[MessageChannel.telegram] = telegram
    service.providers[MessageChannel.sms] = sms
    async with database() as session:
        assert await service.process_pending_messages(session) == 1
        recipient = await session.get(MessageRecipient, recipient_id)
        assert "#" in recipient.rendered_message  # Explicitly authorized raw admin history link.
        if definitive:
            assert recipient.channel == MessageChannel.sms and recipient.status == MessageDeliveryStatus.pending
        else:
            assert recipient.channel == MessageChannel.telegram and recipient.status == MessageDeliveryStatus.failed
            assert recipient.send_started_at is not None
            assert await service.retry_failed(session) == 0
    assert sms.calls == 0 and telegram.calls == 1
    async with database() as session:
        await service.process_pending_messages(session)
    assert telegram.calls == 1


async def seed_recipient(database):
    async with database() as session:
        customer = Customer(name="Synthetic", phone="+380501112297")
        campaign = Campaign(name="Synthetic reminder", type=CampaignType.appointment_reminder,
            status=CampaignStatus.active, channel=MessageChannel.telegram, purpose=MessagePurpose.transactional,
            metadata_json={"message_body": "Synthetic reminder"})
        session.add_all([customer, campaign])
        await session.flush()
        session.add(ClientCommunicationPreference(customer_id=customer.id, telegram_chat_id="synthetic-chat"))
        row = MessageRecipient(campaign_id=campaign.id, customer_id=customer.id,
            channel=MessageChannel.telegram, idempotency_key="synthetic-recipient", rendered_message="Synthetic reminder")
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.parametrize("delivery", ["manual", "generic"])
async def test_dnc_committed_before_customer_admission_prevents_provider_attempt(database, monkeypatch, delivery):
    from fastapi import HTTPException
    from app.api.v1.routes import messaging as routes
    from app.services.messaging import _recipient_delivery_load_options
    recipient_id = await seed_recipient(database)
    provider = Provider()
    service = MessagingService()
    service.providers[MessageChannel.telegram] = provider
    monkeypatch.setattr(routes, "TelegramMessageProvider", lambda: provider)
    async with database() as sender, database() as owner, database() as observer:
        recipient = await sender.scalar(select(MessageRecipient).where(MessageRecipient.id == recipient_id)
            .options(*_recipient_delivery_load_options()))
        customer_id = recipient.customer_id
        cached_preference = await service.get_preference(sender, customer_id)
        assert cached_preference.do_not_contact is False
        sender_pid = await sender.scalar(text("SELECT pg_backend_pid()"))
        owner_pid = await owner.scalar(text("SELECT pg_backend_pid()"))
        await owner.execute(select(Customer).where(Customer.id == customer_id).with_for_update(key_share=True))
        async def deliver():
            if delivery == "manual":
                with pytest.raises(HTTPException) as denied:
                    await routes.send_customer_manual_message(customer_id,
                        routes.ManualCustomerMessageRequest(body="Synthetic marketing", idempotency_key="dnc-admission"),
                        object(), sender)
                assert denied.value.status_code == 400
            else:
                await service.send_recipient(sender, recipient)
                await sender.commit()
        task = asyncio.create_task(deliver())
        try:
            async def wait_until_blocked():
                while True:
                    blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": sender_pid})
                    if owner_pid in blockers:
                        return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_until_blocked(), 5)
            assert provider.calls == 0
            preference = await service.get_preference(owner, customer_id)
            preference.do_not_contact = True
            await owner.commit()
            await asyncio.wait_for(task, 5)
            assert provider.calls == 0
            assert await observer.scalar(select(TelegramDeliveryAttempt.key)) is None
            if delivery == "generic":
                await observer.commit()
                assert (await observer.get(MessageRecipient, recipient_id)).status == MessageDeliveryStatus.skipped
        finally:
            await owner.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_generic_recipient_overlap_has_one_provider_acceptance_and_one_outcome(database):
    recipient_id = await seed_recipient(database)
    provider = Provider(pause=True)
    service = MessagingService()
    service.providers[MessageChannel.telegram] = provider
    async def worker():
        async with database() as session:
            return await service.process_pending_messages(session)
    first = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        async with database() as observer:
            row = await observer.get(MessageRecipient, recipient_id)
            assert row.send_started_at is not None and row.attempts == 1
        await asyncio.wait_for(worker(), 5)
        assert provider.calls == 1
        provider.release.set()
        await asyncio.wait_for(first, 5)
        async with database() as observer:
            row = await observer.get(MessageRecipient, recipient_id)
            assert row.status == MessageDeliveryStatus.sent and row.provider_message_id == "synthetic-accepted"
    finally:
        provider.release.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_accepted_attempt_recovers_recipient_projection_without_provider_repeat(database):
    recipient_id = await seed_recipient(database)
    async with database() as session:
        row = await session.get(MessageRecipient, recipient_id)
        row.send_started_at = datetime.now(UTC)
        row.attempts = 1
        session.add(TelegramDeliveryAttempt(key=f"recipient:{recipient_id}:telegram", state="accepted",
            destination="synthetic-chat", body="Synthetic reminder", attempts=1,
            started_at=datetime.now(UTC), provider_message_id="synthetic-accepted"))
        await session.commit()
    provider = Provider()
    service = MessagingService()
    service.providers[MessageChannel.telegram] = provider
    async with database() as session:
        assert await service.process_pending_messages(session) == 0  # Reconciliation projects before the pending scan.
    async with database() as session:
        row = await session.get(MessageRecipient, recipient_id)
        assert row.status == MessageDeliveryStatus.sent and row.provider_message_id == "synthetic-accepted"
    assert provider.calls == 0


async def test_master_notification_cancellation_persists_ownership_and_suppresses_restart(database, monkeypatch):
    from app.services import master_notifications
    from app.services.master_notifications import MasterCampaignNotificationService, NewBookingTelegram
    from app.core.config import settings
    monkeypatch.setattr(master_notifications, "AsyncSessionLocal", database)
    monkeypatch.setattr(settings, "telegram_bot_token", "synthetic-no-network-token")
    start = datetime(2026, 8, 28, 10, tzinfo=UTC)
    async with database() as session:
        master = Master(full_name="Synthetic master", telegram_chat_id="synthetic-chat")
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Synthetic", duration_minutes=30, price=100)
        session.add(service)
        await session.flush()
        booking = Booking(master_id=master.id, service_id=service.id, customer_name="Synthetic",
            customer_phone="+380501112298", start_at=start, end_at=start + timedelta(minutes=30))
        campaign = Campaign(name="Synthetic master notice", type=CampaignType.master_booking_created,
            status=CampaignStatus.active, channel=MessageChannel.telegram, purpose=MessagePurpose.transactional,
            metadata_json={"message_body": "Synthetic notice {master_name}"})
        session.add_all([booking, campaign])
        await session.commit()
        notification = NewBookingTelegram(booking_id=booking.id, master_id=master.id,
            master_name="Synthetic master", telegram_chat_id="synthetic-chat", service_name="Synthetic",
            customer_name="Synthetic", customer_phone="+380501112298", customer_comment=None,
            start_at=start, end_at=booking.end_at)
    provider = Provider(pause=True)
    service = MasterCampaignNotificationService(provider)
    task = asyncio.create_task(service.send_new_booking_to_master(notification))
    await asyncio.wait_for(provider.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    key = f"master:booking_created:booking:{notification.booking_id}"
    assert await state(database, key) == ("attempting", 1, None)
    await service.send_new_booking_to_master(notification)
    assert provider.calls == 1
    async with database() as session:
        delivery = (await session.scalars(select(MasterMessageDelivery))).one()
        assert delivery.attempts == 1 and delivery.sent_at is None


async def test_schedule_batch_commits_first_outcome_before_second_interruption(database, monkeypatch):
    from app.services.master_schedule_reminders import MasterScheduleReminderService
    from app.core.config import settings
    from app.services.booking import KYIV_TZ
    monkeypatch.setattr(settings, "telegram_bot_token", "synthetic-no-network-token")
    async with database() as session:
        first = Master(full_name="Synthetic first", telegram_chat_id="synthetic-first")
        second = Master(full_name="Synthetic second", telegram_chat_id="synthetic-second")
        campaign = Campaign(name="Synthetic schedule notice", type=CampaignType.master_schedule_reminder,
            status=CampaignStatus.active, channel=MessageChannel.telegram, purpose=MessagePurpose.transactional,
            metadata_json={"message_body": "Synthetic {master_name}"})
        session.add_all([first, second, campaign])
        await session.commit()
        first_id, second_id = first.id, second.id
    class InterruptSecond(Provider):
        async def send_message(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError("synthetic interrupted second send")
            return ProviderSendResult("synthetic-first", {"ok": True})
    provider = InterruptSecond()
    service = MasterScheduleReminderService(provider)
    async def coverage(*args):
        return 120, 30.0
    monkeypatch.setattr(service, "_coverage", coverage)
    when = datetime(2026, 8, 28, 10, tzinfo=KYIV_TZ)
    async with database() as session:
        with pytest.raises(asyncio.CancelledError):
            await service.process_due(session, now=when)
    async with database() as session:
        rows = list(await session.scalars(select(MasterScheduleReminder).order_by(MasterScheduleReminder.master_id)))
        assert len(rows) == 2 and [r.master_id for r in rows] == [first_id, second_id]
        assert rows[0].initial_sent_at is not None and rows[0].initial_provider_message_id == "synthetic-first"
        assert rows[1].initial_sent_at is None and rows[1].initial_attempts == 1
        assert all(r.target_month == date(2026, 9, 1) for r in rows)
    async with database() as session:
        assert await service.process_due(session, now=when) == 0
    assert provider.calls == 2


@pytest.mark.parametrize("multi_service", [False, True])
async def test_review_refresh_preserves_services_and_public_master_for_rendering(database, multi_service):
    from app.models.booking import BookingServiceItem

    recipient_id, _ = await seed_review(database)
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        booking = await session.get(Booking, recipient.appointment_id)
        campaign = await session.get(Campaign, recipient.campaign_id)
        campaign.metadata_json = {"message_body": "{master_name}: {service_name}. {review_link}"}
        public_master = Master(full_name="Public review master")
        session.add(public_master)
        await session.flush()
        booking.redirected_from_master_id = public_master.id
        if multi_service:
            extra = BarberService(master_id=booking.master_id, name="Second service", duration_minutes=15, price=50)
            session.add(extra)
            await session.flush()
            session.add_all([
                BookingServiceItem(booking_id=booking.id, service_id=booking.service_id, position=0, price_amount=100),
                BookingServiceItem(booking_id=booking.id, service_id=extra.id, position=1, price_amount=50),
            ])
        await session.commit()

    provider = Provider()
    service = MessagingService(providers={MessageChannel.telegram: provider})
    async with database() as session:
        assert await service.process_pending_messages(session) == 1
        recipient = await session.get(MessageRecipient, recipient_id)
        assert recipient.status == MessageDeliveryStatus.sent
        expected_services = "Synthetic, Second service" if multi_service else "Synthetic"
        assert recipient.rendered_message.startswith(f"Public review master: {expected_services}. ")
    assert provider.calls == 1
