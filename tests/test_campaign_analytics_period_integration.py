"""Period filters select run cohorts without changing redemption definitions."""
from datetime import UTC, datetime, timedelta
import pytest

from test_segments_integration import database, anyio_backend
from test_campaign_offers_integration import setup_offer, reserve
from app.models.campaign_offer import CampaignOffer
from app.models.campaign_run import CampaignRun
from app.models.booking import Booking, BookingStatus
from app.services.campaign_analytics import offer_analytics
from app.services.promotion import PromotionService


@pytest.mark.anyio
async def test_period_cohort_boundaries_and_completed_financial_snapshots(database):
    ids = await setup_offer(database)
    at = datetime(2026, 9, 1, 10, tzinfo=UTC)
    async with database() as session:
        offer = await session.get(CampaignOffer, ids[-2])
        run = await session.get(CampaignRun, offer.run_id)
        run.evaluated_at = at
        run.campaign_snapshot = {'offer_master_id':ids[1]}
        run.audience_count = 1
        offer.conditions_snapshot = {**offer.conditions_snapshot, 'communication_eligible':True}
        await session.commit()
    booking_id = await reserve(database, ids)
    async with database() as session:
        booking = await session.get(Booking, booking_id)
        await PromotionService().sync_first_visit_status(session, booking, BookingStatus.completed)
        booking.status = BookingStatus.completed
        await session.commit()
        included = await offer_analytics(session, master_id=ids[1], date_from=at, date_to=at+timedelta(days=1))
        assert included['audience_size'] == included['communication_eligible_recipients'] == 1
        assert included['bookings_created'] == included['completed_visits'] == included['unique_customers_redeemed'] == 1
        assert included['total_discount_amount'] == 240
        assert included['observed_completed_revenue'] == 560
        assert included['booking_conversion_percent'] == included['redemption_conversion_percent'] == 100
        assert included['period_basis'] == 'run_cohort'
        before = await offer_analytics(session, date_to=at)
        after = await offer_analytics(session, date_from=at+timedelta(seconds=1))
        for excluded in (before, after):
            assert excluded['audience_size'] == excluded['communication_eligible_recipients'] == 0
            assert excluded['completed_visits'] == excluded['total_discount_amount'] == excluded['observed_completed_revenue'] == 0
            assert excluded['redemption_conversion_percent'] is None
