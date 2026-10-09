"""Campaign-run Telegram acceptance survives projection interruption."""
import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from test_inventory_integration import anyio_backend, database  # noqa: F401
from app.models.campaign_run import CampaignRun
from app.models.customer import Customer
from app.models.messaging import (Campaign, CampaignStatus, CampaignType, ClientCommunicationPreference,
    ConsentStatus, MessageChannel, MessageDeliveryStatus, MessageLog, MessagePurpose, MessageRecipient,
    TelegramDeliveryAttempt)
from app.services.campaign_runs import CampaignRunService
from app.services.messaging import MessagingService, ProviderSendResult

pytestmark = pytest.mark.anyio


class Telegram:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure
    async def send_message(self, **kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        return ProviderSendResult("synthetic-campaign-accepted", {"ok": True})


class SmsMustNotSend:
    uses_durable_queue = True
    async def enqueue_message(self, *args, **kwargs):
        raise AssertionError("Uncertain Telegram delivery must not enqueue SMS fallback")
    async def send_message(self, **kwargs):
        raise AssertionError("Uncertain Telegram delivery must not send SMS fallback")


async def seed(database):
    async with database() as session:
        customer = Customer(name="Synthetic", phone="+380501112293")
        campaign = Campaign(name="Synthetic recovery run", type=CampaignType.manual,
            status=CampaignStatus.active, channel=MessageChannel.telegram, purpose=MessagePurpose.marketing,
            metadata_json={"message_body": "Synthetic campaign message"})
        session.add_all([customer, campaign])
        await session.flush()
        session.add(ClientCommunicationPreference(customer_id=customer.id, telegram_chat_id="synthetic-chat",
            marketing_consent=ConsentStatus.opted_in))
        run = CampaignRun(campaign_id=campaign.id, idempotency_key="synthetic-recovery-run", status="running",
            evaluated_at=datetime.now(UTC), audience_count=1,
            campaign_snapshot={"channel": "telegram", "purpose": "marketing", "channel_strategy": "telegram_then_sms",
                "sending_window": {"start": "00:00", "end": "23:59", "days": list(range(7))}})
        session.add(run)
        await session.flush()
        recipient = MessageRecipient(campaign_id=campaign.id, run_id=run.id, customer_id=customer.id,
            channel=MessageChannel.telegram, idempotency_key="synthetic-campaign-recipient",
            rendered_message="Synthetic campaign message")
        session.add(recipient)
        await session.commit()
        return recipient.id, customer.id, campaign.id, run.id


async def test_campaign_acceptance_then_projection_cancel_recovers_even_after_pause_and_consent_withdrawal(database, monkeypatch):
    recipient_id, customer_id, campaign_id, run_id = await seed(database)
    provider = Telegram()
    messaging = MessagingService()
    messaging.providers[MessageChannel.telegram] = provider
    messaging.providers[MessageChannel.sms] = SmsMustNotSend()
    runs = CampaignRunService(messaging)
    original_log = messaging._log_from_recipient
    def interrupt_projection(recipient, status, **kwargs):
        if status == MessageDeliveryStatus.sent:
            raise asyncio.CancelledError("synthetic projection interruption after durable acceptance")
        return original_log(recipient, status, **kwargs)
    monkeypatch.setattr(messaging, "_log_from_recipient", interrupt_projection)
    async with database() as session:
        with pytest.raises(asyncio.CancelledError):
            await runs.send_recipient(session, await session.get(MessageRecipient, recipient_id))
    async with database() as session:
        row = await session.get(MessageRecipient, recipient_id)
        assert row.status == MessageDeliveryStatus.pending and row.provider_message_id is None
        attempt = await session.get(TelegramDeliveryAttempt, f"recipient:{recipient_id}:telegram")
        assert attempt.state == "accepted" and attempt.provider_message_id == "synthetic-campaign-accepted"
        campaign = await session.get(Campaign, campaign_id)
        campaign.status = CampaignStatus.paused
        preference = (await session.scalars(select(ClientCommunicationPreference).where(
            ClientCommunicationPreference.customer_id == customer_id))).one()
        preference.do_not_contact = True
        preference.marketing_consent = ConsentStatus.opted_out
        await session.commit()
    monkeypatch.setattr(messaging, "_log_from_recipient", original_log)
    async with database() as session:
        assert await runs.process_run_messages(session) == 0
        row = await session.get(MessageRecipient, recipient_id)
        assert row.status == MessageDeliveryStatus.sent and row.provider_message_id == "synthetic-campaign-accepted"
        assert row.sent_at is not None and row.attempts == 1
        assert (await session.get(CampaignRun, run_id)).status == "completed"
        logs = list(await session.scalars(select(MessageLog).where(MessageLog.recipient_id == recipient_id)))
        assert len(logs) == 1 and logs[0].status == MessageDeliveryStatus.sent
        assert await runs.reconcile_interrupted_sends(session) == 0
    assert provider.calls == 1


async def test_campaign_unknown_attempt_never_retries_or_queues_sms_on_restart(database):
    recipient_id, _, _, _ = await seed(database)
    provider = Telegram(failure=TimeoutError("synthetic uncertain acceptance"))
    messaging = MessagingService()
    messaging.providers[MessageChannel.telegram] = provider
    messaging.providers[MessageChannel.sms] = SmsMustNotSend()
    runs = CampaignRunService(messaging)
    async with database() as session:
        await runs.send_recipient(session, await session.get(MessageRecipient, recipient_id))
    async with database() as session:
        row = await session.get(MessageRecipient, recipient_id)
        assert row.status == MessageDeliveryStatus.failed and row.sms_queue_job_id is None
        assert row.send_started_at is not None
        assert (await session.get(TelegramDeliveryAttempt, f"recipient:{recipient_id}:telegram")).state == "ambiguous"
        await runs.send_recipient(session, row)
        assert await messaging.retry_failed(session) == 0
    assert provider.calls == 1
