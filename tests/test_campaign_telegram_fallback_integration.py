"""Real offer snapshot and durable fallback queue against isolated PostgreSQL."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_segments_integration import database, anyio_backend, add_campaign, add_customer, add_segment, KYIV
from test_sms_queue_integration import sms_transport, SimulatedClock, campaign_worker
from app.models.booking import Master, BarberService
from app.models.campaign_run import CampaignRun
from app.models.messaging import Campaign, CampaignStatus, ClientCommunicationPreference, MessageChannel, MessageDeliveryStatus, MessageRecipient
from app.models.promotion import Promotion
from app.services.messaging import MessageProvider, TelegramDestinationUnavailable


@pytest.mark.anyio
@pytest.mark.parametrize('definitive', [True, False])
async def test_telegram_rejection_queues_sms_but_uncertain_outcome_does_not(database, sms_transport, monkeypatch, definitive):
    clock = SimulatedClock()
    clock.value = datetime.now(KYIV).replace(hour=10, minute=5, second=0, microsecond=0).astimezone(UTC)
    import app.services.campaign_runs as module

    class Daytime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.value.astimezone(tz) if tz else clock.value.replace(tzinfo=None)
    monkeypatch.setattr(module, 'datetime', Daytime)

    class RejectedTelegram(MessageProvider):
        channel = MessageChannel.telegram
        calls = 0
        async def send_message(self, **kwargs):
            self.calls += 1
            if definitive:
                raise TelegramDestinationUnavailable('blocked test destination')
            raise TimeoutError('uncertain test outcome')

    runs, queue, _ = campaign_worker(database, clock)
    telegram = RejectedTelegram()
    runs.messaging.providers[MessageChannel.telegram] = telegram
    async def ready(*args, **kwargs): return {'ready': True}
    monkeypatch.setattr(runs, 'readiness', ready)  # This test isolates restriction/transport, not worker health.
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=clock.value-timedelta(days=180))
        other = await add_customer(session, 2, imported_last_visit_at=clock.value-timedelta(days=180))
        recent = await add_customer(session, 3, imported_last_visit_at=clock.value-timedelta(days=1))
        session.add(ClientCommunicationPreference(customer_id=customer.id, telegram_chat_id='sandbox-chat', marketing_consent='opted_in'))
        master = Master(full_name='Інший майстер')
        promotion = Promotion(code='fallback-test', name_uk='Новий майстер', name_en='New Master', discount_percent=30, recipient_offer_only=True)
        session.add_all([master, promotion]); await session.flush()
        service = BarberService(master_id=master.id, name='Haircut', price=800, duration_minutes=30)
        session.add(service); await session.flush()
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment])
        campaign.metadata_json = {**campaign.metadata_json, 'channel_strategy': 'telegram_then_sms'}
        campaign.offer_master_id = master.id
        campaign.offer_promotion_id = promotion.id
        campaign.offer_service_ids = [service.id]
        campaign.offer_starts_at = clock.value-timedelta(days=1)
        campaign.offer_expires_at = clock.value+timedelta(days=14)
        campaign.master_name_for_message = 'Іншого майстра'
        campaign.sending_window = {'start':'10:00', 'end':'18:00', 'days':list(range(7))}
        campaign.sms_recipients_per_minute = 20
        campaign.template.body = '{{master_name}}: {{offer_link}}'
        campaign.status = CampaignStatus.active
        await session.commit()
        campaign_id, customer_id, other_id, recent_id = campaign.id, customer.id, other.id, recent.id
        with pytest.raises(HTTPException) as rejected:
            await runs.launch(session, campaign, idempotency_key='ineligible-test', test_customer_id=recent_id)
        assert rejected.value.detail['code'] == 'test_recipient_ineligible'
        await session.rollback()
        campaign = await session.get(Campaign, campaign_id)
        run = await runs.launch(session, campaign, idempotency_key='fallback-test-run', test_customer_id=customer_id)
        run_id = run.id
        assert run.audience_count == 1
        assert run.campaign_snapshot['test_customer_id'] == customer_id
        again = await runs.launch(session, campaign, idempotency_key='fallback-test-run', test_customer_id=customer_id)
        assert again.id == run_id
        with pytest.raises(HTTPException) as changed:
            await runs.launch(session, campaign, idempotency_key='fallback-test-run', test_customer_id=other_id)
        assert changed.value.detail['code'] == 'launch_idempotency_conditions_changed'
        await session.rollback()
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id==run_id))).one()
        recipient_id = recipient.id
        await runs.send_recipient(session, recipient)
    assert telegram.calls == 1
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        if definitive:
            assert recipient.channel == MessageChannel.sms
            assert recipient.sms_queue_job_id is not None
            assert recipient.status == MessageDeliveryStatus.pending
        else:
            assert recipient.sms_queue_job_id is None
            assert recipient.status == MessageDeliveryStatus.failed
    if definitive:
        assert await queue.process_one()
        assert len(sms_transport.requests) == 1
        async with database() as session:
            assert (await session.get(MessageRecipient, recipient_id)).status == MessageDeliveryStatus.sent
    else:
        assert sms_transport.requests == []
