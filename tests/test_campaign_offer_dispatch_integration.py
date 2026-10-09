"""Personalized offers through the durable queue; disposable PostgreSQL and fake HTTP only."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.booking import Master, BarberService
from app.models.campaign_run import CampaignRun
from app.models.messaging import CampaignStatus, ClientCommunicationPreference, ConsentStatus, MessageRecipient
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
        session.add(ClientCommunicationPreference(customer_id=customer.id, marketing_consent=ConsentStatus.opted_in))
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


@pytest.mark.anyio
@pytest.mark.parametrize('channel,strategy', [('sms', 'single'), ('telegram', 'single'), ('telegram', 'telegram_then_sms')])
async def test_segment_offer_snapshots_selected_discount_without_legacy_audience(database, channel, strategy):
    from app.models.messaging import MessageChannel
    from app.schemas.segment import SegmentRules
    at = datetime.now(KYIV)
    async with database() as session:
        unknown = await add_customer(session, 1)
        recent = await add_customer(session, 2, imported_last_visit_at=at - timedelta(days=10))
        master = Master(full_name='Configurable master')
        promotion = Promotion(code='PERSONAL_TEST', name_uk='Personal UK', name_en='Personal EN',
                              discount_percent=15, recipient_offer_only=True)
        session.add_all([master, promotion])
        await session.flush()
        service = BarberService(master_id=master.id, name='Haircut', price=800, duration_minutes=30)
        session.add(service)
        await session.flush()
        # Unknown/imported-only histories intentionally cannot match a visit-count rule.
        segment = await add_segment(session, rules=SegmentRules(conditions=[{'type': 'upcoming_booking', 'present': False}]))
        campaign = await add_campaign(session, [segment], offer_audience_mode='segments', channel_strategy=strategy)
        campaign.channel = MessageChannel(channel)
        campaign.offer_master_id = master.id
        campaign.offer_promotion_id = promotion.id
        campaign.offer_service_ids = [service.id]
        campaign.offer_starts_at = at - timedelta(days=1)
        campaign.offer_expires_at = at + timedelta(days=14)
        campaign.master_name_for_message = 'Master'
        campaign.template.body = '{{discount_percent}}% {{promotion_name_uk}} {{promotion_name_en}} {{offer_link}}'
        run = CampaignRun(campaign_id=campaign.id, idempotency_key='configurable-snapshot', status='scheduled',
                          audience_count=0, campaign_snapshot={}, segment_snapshots=[])
        session.add(run)
        await session.flush()
        runs = CampaignRunService()
        await runs.snapshot(session, run, campaign, now=at)
        await session.commit()
        offers = list((await session.scalars(select(CampaignOffer).where(CampaignOffer.run_id == run.id))).all())
        recipients = list((await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id))).all())
        assert {offer.customer_id for offer in offers} == {unknown.id, recent.id}
        assert all(offer.discount_percent == 15 for offer in offers)
        assert all(offer.conditions_snapshot['offer_audience_mode'] == 'segments' for offer in offers)
        assert all('15% Personal UK Personal EN' in recipient.rendered_message for recipient in recipients)
        assert run.campaign_snapshot['offer_discount_percent'] == 15
        assert run.segment_snapshots[0]['revision'] == segment.revision
        # Editing live definitions cannot change issued entitlement or message.
        promotion.discount_percent = 80
        segment.rules = {'combine': 'all', 'conditions': [{'type': 'completed_visit_count', 'min': 100}], 'exclusions': []}
        campaign.metadata_json = {**campaign.metadata_json, 'offer_audience_mode': 'last_visit_3_12'}
        await session.commit()
        restrictions = [await campaign_offer_service.current_restriction(session, offer, at=at) for offer in offers]
        assert restrictions == [None, None]
        assert all(offer.discount_percent == 15 for offer in offers)


@pytest.mark.anyio
@pytest.mark.parametrize('exclude_upcoming_booking', [True, False])
async def test_segment_offer_frozen_communication_eligibility_matches_upcoming_exclusion(database, exclude_upcoming_booking):
    from test_segments_integration import add_booking
    from app.models.booking import BookingStatus
    from app.schemas.segment import SegmentRules
    from app.services.campaign_analytics import offer_analytics
    at = datetime.now(KYIV)
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=at - timedelta(days=10))
        session.add(ClientCommunicationPreference(customer_id=customer.id, marketing_consent=ConsentStatus.opted_in))
        master = Master(full_name='Configurable master')
        promotion = Promotion(code='PERSONAL_TEST', name_uk='Personal UK', name_en='Personal EN',
                              discount_percent=15, recipient_offer_only=True)
        session.add_all([master, promotion])
        await session.flush()
        service = BarberService(master_id=master.id, name='Haircut', price=800, duration_minutes=30)
        session.add(service)
        await session.flush()
        await add_booking(session, customer, at + timedelta(days=2), BookingStatus.confirmed, master=master, service=service)
        segment = await add_segment(session, rules=SegmentRules(conditions=[{'type': 'last_visit_age', 'max': 30, 'unit': 'days'}]))
        campaign = await add_campaign(session, [segment], offer_audience_mode='segments',
                                      exclude_upcoming_booking=exclude_upcoming_booking)
        campaign.offer_master_id = master.id
        campaign.offer_promotion_id = promotion.id
        campaign.offer_service_ids = [service.id]
        campaign.offer_starts_at = at - timedelta(days=1)
        campaign.offer_expires_at = at + timedelta(days=14)
        campaign.master_name_for_message = 'Master'
        campaign.template.body = '{{discount_percent}}% {{offer_link}}'
        run = CampaignRun(campaign_id=campaign.id, idempotency_key='configurable-exclusion', status='scheduled',
                          audience_count=0, campaign_snapshot={}, segment_snapshots=[])
        session.add(run)
        await session.flush()
        runs = CampaignRunService()
        await runs.snapshot(session, run, campaign, now=at)
        await session.commit()
        offer = (await session.scalars(select(CampaignOffer).where(CampaignOffer.run_id == run.id))).one()
        assert offer.customer_id == customer.id
        assert run.audience_count == 1
        assert offer.conditions_snapshot['communication_eligible'] is (not exclude_upcoming_booking)
        preview = await runs.preview(session, campaign)
        assert preview['total'] == 1
        assert preview['items'][0]['eligible'] is (not exclude_upcoming_booking)
        assert preview['items'][0]['exclusion_reason'] == ('upcoming_booking' if exclude_upcoming_booking else None)
        assert preview['communication_eligible_recipients'] == int(not exclude_upcoming_booking)
        analytics = await offer_analytics(session, run_id=run.id)
        assert analytics['audience_size'] == 1
        assert analytics['communication_eligible_recipients'] == int(not exclude_upcoming_booking)
