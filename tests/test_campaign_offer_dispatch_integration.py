"""Personalized offers through the durable queue; disposable PostgreSQL and fake HTTP only."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.booking import Master, BarberService
from app.models.campaign_run import CampaignRun
from app.models.messaging import CampaignStatus, MessageRecipient
from app.models.campaign_offer import CampaignOffer
from app.models.promotion import Promotion
from app.services.campaign_runs import CampaignRunService, offer_options, sms_parts
from app.services.campaign_offers import campaign_offer_service
from test_segments_integration import database, anyio_backend, add_campaign, add_customer, add_segment, KYIV
from test_sms_queue_integration import sms_transport, SimulatedClock, campaign_worker


@pytest.mark.anyio
async def test_snapshot_personalization_and_retry_dispatch_rechecks_offer(database, sms_transport):
    clock = SimulatedClock()
    clock.value = datetime.now(KYIV).replace(hour=10, minute=0, second=0, microsecond=0).astimezone(UTC)
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=clock.value - timedelta(days=180))
        master = Master(full_name='Тестовий майстер')
        promotion = Promotion(code='new-master-sandbox', name_uk='Новий майстер', name_en='New Master',
                              discount_percent=30, recipient_offer_only=True)
        session.add_all([master, promotion])
        await session.flush()
        service = BarberService(master_id=master.id, name='Haircut', price=800, duration_minutes=30)
        session.add(service)
        await session.flush()
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment])
        campaign.offer_master_id = master.id
        campaign.offer_promotion_id = promotion.id
        campaign.offer_service_ids = [service.id]
        campaign.offer_starts_at = clock.value - timedelta(days=1)
        campaign.offer_expires_at = clock.value + timedelta(days=14)
        campaign.master_name_for_message = 'Тестового майстра'
        campaign.sending_window = {'start': '10:00', 'end': '18:00', 'days': list(range(7)), 'timezone': 'Europe/Kyiv'}
        campaign.sms_recipients_per_minute = 20
        campaign.template.body = '{{master_name}} до {{offer_expires_short}}: {{offer_link}}'
        campaign.status = CampaignStatus.active
        run = CampaignRun(campaign_id=campaign.id, idempotency_key='offer-dispatch-sandbox', status='scheduled',
                          audience_count=0, campaign_snapshot={}, segment_snapshots=[])
        session.add(run)
        await session.flush()
        await CampaignRunService().snapshot(session, run, campaign, now=clock.value)
        await session.commit()
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id))).one()
        offer = (await session.scalars(select(CampaignOffer).where(CampaignOffer.recipient_id == recipient.id))).one()
        assert 'Тестового майстра' in recipient.rendered_message
        assert 'offer_token=' in recipient.rendered_message
        assert str(customer.phone) not in recipient.rendered_message
        assert offer.conditions_snapshot['communication_eligible'] is True
        assert offer.conditions_snapshot['eligibility_facts']['history_state'] == 'known'
        assert offer.conditions_snapshot['eligibility_facts']['last_visit_at']
        assert offer.conditions_snapshot['eligibility_facts']['has_upcoming_booking'] is False
        preview = await CampaignRunService().preview(session, campaign)
        assert preview['sms_eligible_recipients'] == 1
        assert preview['items'][0]['sms_parts'] == sms_parts(recipient.rendered_message)
        recipient_id = recipient.id
    runs, queue, dispatch = campaign_worker(database, clock)
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        from app.models.messaging import MessageChannel
        await runs.messaging.enqueue_sms_recipient(session, recipient, runs.messaging.providers[MessageChannel.sms],
                                                   customer.phone, recipient.rendered_message, priority=100)
        await session.commit()
    # Expiry changes after enqueue must still be checked before transport.
    async with database() as session:
        offer = (await session.scalars(select(CampaignOffer).where(CampaignOffer.recipient_id == recipient_id))).one()
        offer.expires_at = clock.value - timedelta(seconds=1)
        await session.commit()
    assert await queue.process_one()
    assert sms_transport.requests == []
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        assert recipient.last_error == 'offer_expired_or_not_started'


@pytest.mark.anyio
async def test_all_clients_offer_audience_still_enforces_known_inactivity_and_no_future_booking(database):
    from test_segments_integration import add_booking
    from app.models.booking import BookingStatus
    at = datetime(2026, 10, 5, 12, tzinfo=KYIV)
    async with database() as session:
        # A broad inline audience must never issue an offer to unknown/recent/too-old history.
        await add_customer(session, 1)
        await add_customer(session, 2, imported_last_visit_at=at - timedelta(days=30))
        await add_customer(session, 3, imported_last_visit_at=at - timedelta(days=400))
        future = await add_customer(session, 4, imported_last_visit_at=at - timedelta(days=180))
        await add_booking(session, future, at + timedelta(days=1), status=BookingStatus.confirmed)
        master = Master(full_name='Sandbox offer master')
        session.add(master)
        await session.flush()
        campaign = await add_campaign(session, [])
        campaign.offer_master_id = master.id
        await session.flush()
        service = CampaignRunService()
        assert list((await session.scalars(service._audience_statement(campaign, [], at))).all()) == []
        # Exact calendar boundaries are eligible independently of the selected broad audience.
        three = await add_customer(session, 5, imported_last_visit_at=datetime(2026, 7, 5, 12, tzinfo=KYIV))
        twelve = await add_customer(session, 6, imported_last_visit_at=datetime(2025, 10, 5, 12, tzinfo=KYIV))
        matches = list((await session.scalars(service._audience_statement(campaign, [], at))).all())
        assert {item.id for item in matches} == {three.id, twelve.id}
