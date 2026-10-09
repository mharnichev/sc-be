"""Issued entitlements outlive a paused/archived campaign or cancelled run."""
import pytest
from sqlalchemy import select
from test_campaign_offers_integration import setup_offer, reserve, database, anyio_backend
from app.models.campaign_offer import CampaignOffer
from app.models.campaign_run import CampaignRun
from app.models.messaging import Campaign, CampaignStatus, MessageRecipient
from app.models.booking import Booking, BookingStatus
from app.services.promotion import PromotionService
from app.services.campaign_runs import CampaignRunService

@pytest.mark.anyio
@pytest.mark.parametrize("action", ["paused", "archived", "cancelled-run"])
async def test_issued_offer_remains_redeemable_until_its_own_expiry(database, action):
    ids = await setup_offer(database)
    async with database() as session:
        recipient = (await session.scalars(select(MessageRecipient))).one()
        offer = await session.get(CampaignOffer, ids[-2])
        original_expiry = offer.expires_at
        campaign = await session.get(Campaign, recipient.campaign_id)
        if action == "cancelled-run":
            await CampaignRunService().cancel_run_unsent(session, await session.get(CampaignRun, recipient.run_id))
        else:
            campaign.status = CampaignStatus(action)
            await session.commit()
    booking_id = await reserve(database, ids)
    async with database() as session:
        offer = await session.get(CampaignOffer, ids[-2])
        booking = await session.get(Booking, booking_id)
        assert offer.expires_at == original_expiry
        assert booking.campaign_offer_reservation_id == offer.id
        assert offer.consumed_booking_id is None
        await PromotionService().sync_first_visit_status(session, booking, BookingStatus.completed)
        booking.status = BookingStatus.completed
        await session.commit()
        assert offer.consumed_booking_id == booking_id
        assert booking.total_amount == 560
        assert booking.promotion_discount_amount == 240
