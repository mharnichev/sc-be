"""Disposable PostgreSQL-only seed, calendar boundaries and aggregation checks."""
from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import select, func
from app.models.booking import Booking, BookingStatus, Master, BarberService
from app.models.campaign_offer import CampaignOffer
from app.models.campaign_run import CampaignRun
from app.models.messaging import MessageRecipient, MessageTemplate, Campaign, CampaignType, MessageChannel, MessageDeliveryStatus
from app.models.promotion import Promotion
from app.services.campaign_analytics import offer_analytics
from app.services.segments import SegmentService, subtract_age
from app.utils.seed_new_master_campaign import initialize, RULES
from tests.test_segments_integration import database, anyio_backend, add_customer


@pytest.mark.anyio
async def test_exact_inclusive_calendar_boundaries_imported_unknown_and_future(database):
    at = datetime(2026, 10, 5, 12, tzinfo=UTC)
    async with database() as session:
        candidates = [subtract_age(at, 3, 'calendar_months'), subtract_age(at, 12, 'calendar_months'),
                      subtract_age(at, 3, 'calendar_months')+timedelta(microseconds=1),
                      subtract_age(at, 12, 'calendar_months')-timedelta(microseconds=1), None]
        people = [await add_customer(session, i+1, imported_last_visit_at=value) for i, value in enumerate(candidates)]
        stmt = select(type(people[0]).id).where(SegmentService().build_predicate(RULES, at))
        assert set((await session.scalars(stmt)).all()) == {people[0].id, people[1].id}


@pytest.mark.anyio
async def test_seed_idempotent_draft_missing_validity_no_recipients(database):
    async with database() as session:
        session.add(Master(id=4, full_name='Андрій Віканов'))
        await session.flush()
        session.add(BarberService(master_id=4, name='Haircut', duration_minutes=30, price=800))
        await session.commit()
        first = await initialize(session)
        await session.commit()
        second = await initialize(session)
        await session.commit()
        assert first == second and first['status'] == 'draft' and first['validity_required']
        assert await session.scalar(select(func.count()).select_from(MessageRecipient)) == 0
        assert await session.scalar(select(func.count()).select_from(CampaignRun)) == 0
        assert await session.scalar(select(func.count()).select_from(Promotion)) == 1
        assert await session.scalar(select(func.count()).select_from(MessageTemplate)) == 1
        from app.api.v1.routes.messaging import duplicate_campaign
        copied = await duplicate_campaign(first['campaign_id'], None, session)
        assert copied.offer_master_id == 4
        assert copied.offer_promotion_id == first['promotion_id']
        assert copied.segment_ids == [first['segment_id']]
        assert copied.status.value == 'draft' and copied.offer_expires_at is None


@pytest.mark.anyio
async def test_analytics_sources_no_join_multiplication_and_accepted_survives_failure(database):
    now = datetime.now(UTC)
    async with database() as session:
        customer = await add_customer(session, 1)
        master = Master(full_name='Analytics master')
        promotion = Promotion(code='ANALYTICS_OFFER', name_uk='Новий майстер', name_en='New Master', discount_percent=30, recipient_offer_only=True)
        campaign = Campaign(name='Analytics', type=CampaignType.manual, channel=MessageChannel.sms)
        session.add_all([master, promotion, campaign]); await session.flush()
        service = BarberService(master_id=master.id, name='Haircut', duration_minutes=30, price=800)
        run = CampaignRun(campaign_id=campaign.id, idempotency_key='analytics-test', audience_count=3, campaign_snapshot={'offer_master_id':1})
        session.add_all([service, run]); await session.flush()
        recipient = MessageRecipient(campaign_id=campaign.id, run_id=run.id, customer_id=customer.id, idempotency_key='analytics-person',
                                     status=MessageDeliveryStatus.failed, sent_at=now, channel=MessageChannel.sms)
        session.add(recipient); await session.flush()
        offer = CampaignOffer(campaign_id=campaign.id, run_id=run.id, recipient_id=recipient.id, customer_id=customer.id,
            master_id=master.id, promotion_id=promotion.id, token_hash='a'*64, allowed_service_ids=[service.id],
            starts_at=now, expires_at=now+timedelta(days=7), promotion_name_uk='Новий майстер', promotion_name_en='New Master',
            raw_link_requests=5, confirmed_open_count=2, first_confirmed_open_at=now, conditions_snapshot={'communication_eligible':True})
        session.add(offer); await session.flush()
        for status in [BookingStatus.cancelled, BookingStatus.completed]:
            booking = Booking(master_id=master.id, service_id=service.id, customer_id=customer.id, customer_name='A', customer_phone=customer.phone,
                start_at=now, end_at=now+timedelta(minutes=30), status=status, campaign_offer_id=offer.id,
                promotion_discount_amount=240, subtotal_amount=800, total_amount=560)
            session.add(booking)
            await session.flush()
            if status == BookingStatus.completed:
                offer.consumed_booking_id = booking.id
                offer.completed_at = now
                offer.redeemed_discount_amount = 240
                offer.redeemed_revenue_amount = 560
        await session.flush()
        metrics = await offer_analytics(session, campaign_id=campaign.id)
        assert metrics['audience_size'] == 3
        assert metrics['communication_eligible_recipients'] == 1
        assert metrics['provider_accepted'] == 1 and metrics['delivered'] == 0
        assert metrics['raw_link_requests'] == 5 and metrics['confirmed_page_opens'] == 2
        assert metrics['unique_confirmed_openers'] == 1
        assert metrics['bookings_created'] == 2 and metrics['completed_visits'] == 1
        assert metrics['unique_customers_redeemed'] == 1
        assert metrics['total_discount_amount'] == 240 and metrics['observed_completed_revenue'] == 560
        assert (await offer_analytics(session, master_id=999))['booking_conversion_percent'] is None


@pytest.mark.anyio
async def test_new_migration_roundtrip_preserves_legacy_campaign_and_transactional_recipient(database):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from app.models.messaging import MessagePurpose
    async with database() as session:
        customer = await add_customer(session, 1)
        campaign = Campaign(name='Legacy transactional', type=CampaignType.booking_confirmation,
                            purpose=MessagePurpose.transactional, channel=MessageChannel.sms,
                            metadata_json={'sms_recipients_per_minute':13})
        session.add(campaign); await session.flush()
        recipient = MessageRecipient(campaign_id=campaign.id, customer_id=customer.id,
                                     idempotency_key='legacy-transactional', channel=MessageChannel.sms)
        session.add(recipient); await session.commit()
        identifier = campaign.id
        spec = importlib.util.spec_from_file_location('new_master_migration', Path(__file__).parents[1] / 'alembic/versions/0099_new_master_campaign_offers.py')
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        def run(connection, direction):
            with Operations.context(MigrationContext.configure(connection, opts={'target_metadata':Campaign.metadata})):
                getattr(migration, direction)()
        connection = await session.connection()
        await connection.run_sync(run, 'downgrade')
        assert await session.scalar(text('SELECT name FROM campaigns WHERE id=:id'), {'id':identifier}) == 'Legacy transactional'
        await connection.run_sync(run, 'upgrade')
        assert await session.scalar(text('SELECT offer_master_id FROM campaigns WHERE id=:id'), {'id':identifier}) is None
        assert await session.scalar(text("SELECT count(*) FROM message_recipients WHERE idempotency_key='legacy-transactional'")) == 1
