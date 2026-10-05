"""Opt-in disposable PostgreSQL entitlement/conversion checks, with no provider calls."""
import asyncio
from datetime import UTC, datetime, timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.test_segments_integration import database, anyio_backend
from app.models.booking import BarberService, Booking, BookingStatus, Master
from app.models.customer import Customer
from app.models.messaging import Campaign, CampaignType, MessageRecipient
from app.models.campaign_run import CampaignRun
from app.models.campaign_offer import CampaignOffer
from app.models.promotion import Promotion
from app.services.campaign_offers import campaign_offer_service
from app.services.promotion import PromotionService


async def setup_offer(database):
    now = datetime.now(UTC)
    async with database() as session:
        customer = Customer(phone='+380501111111', name='Test', imported_last_visit_at=now-timedelta(days=150))
        master = Master(full_name='Test Master')
        campaign = Campaign(name='Test offer',type=CampaignType.manual)
        promotion = Promotion(code='TEST_OFFER', name_uk='Новий майстер', name_en='New Master',discount_percent=30)
        session.add_all([customer, master, campaign, promotion]); await session.flush()
        service = BarberService(master_id=master.id,name='Haircut',price=800,duration_minutes=30)
        run = CampaignRun(campaign_id=campaign.id,idempotency_key='test-run')
        session.add_all([service,run]);await session.flush()
        recipient=MessageRecipient(campaign_id=campaign.id,run_id=run.id,customer_id=customer.id,idempotency_key='recipient')
        session.add(recipient);await session.flush()
        offer,token = await campaign_offer_service.issue_offer(session,recipient,dict(offer_master_id=master.id,
            offer_promotion_id=promotion.id,offer_starts_at=(now-timedelta(days=1)).isoformat(),
            offer_expires_at=(now+timedelta(days=10)).isoformat(),offer_service_ids=[service.id]))
        await session.commit()
        return customer.id,master.id,service.id,offer.id,token


async def reserve(database, ids):
    customer_id,master_id,service_id,offer_id,token=ids
    async with database() as session:
        booking=Booking(master_id=master_id,service_id=service_id,customer_id=customer_id,
            customer_name='Test',customer_phone='+380501111111',start_at=datetime.now(UTC)+timedelta(days=1),
            end_at=datetime.now(UTC)+timedelta(days=1,minutes=30),status=BookingStatus.confirmed)
        await campaign_offer_service.apply(session,booking=booking,customer=await session.get(Customer,customer_id),
            services=[await session.get(BarberService,service_id)],token=token)
        session.add(booking);await session.commit()
        return booking.id


@pytest.mark.anyio
@pytest.mark.parametrize("release_status", [BookingStatus.cancelled, BookingStatus.no_show])
async def test_concurrent_release_completion_and_idempotent_opens(database, release_status):
    ids=await setup_offer(database)
    results=await asyncio.gather(reserve(database,ids),reserve(database,ids),return_exceptions=True)
    booking_id=next(value for value in results if isinstance(value,int))
    assert sum(isinstance(value,HTTPException) for value in results)==1
    async with database() as session:
        booking=await session.get(Booking,booking_id)
        assert booking.total_amount==560
        await PromotionService().sync_first_visit_status(session,booking,release_status)
        booking.status=release_status
        await session.commit()
    second_id=await reserve(database,ids)
    async with database() as session:
        booking=await session.get(Booking,second_id)
        await PromotionService().sync_first_visit_status(session,booking,BookingStatus.completed)
        booking.status=BookingStatus.completed
        await session.commit()
    with pytest.raises(HTTPException):
        await reserve(database,ids)
    async with database() as session:
        for _ in range(2):
            await campaign_offer_service.context(session,ids[-1],event_id='stable-event-12345')
        await campaign_offer_service.context(session,ids[-1])
        await session.commit()
        offer=await session.get(CampaignOffer,ids[-2])
        assert offer.confirmed_open_count==1
        assert offer.raw_link_requests==1
        assert offer.consumed_booking_id==second_id


@pytest.mark.anyio
async def test_revalidation_preserves_prices_and_rejects_reschedule_after_expiry(database):
    ids=await setup_offer(database)
    booking_id=await reserve(database,ids)
    async with database() as session:
        booking=await session.get(Booking,booking_id)
        # Revalidation loads services as existing route consumers do.
        from sqlalchemy.orm import selectinload
        booking=(await session.execute(select(Booking).options(selectinload(Booking.service), selectinload(Booking.service_items)).where(Booking.id==booking_id))).scalar_one()
        booking.manual_discount_amount=17
        booking.subtotal_amount=1200
        booking.promotion_discount_amount=360
        booking.total_amount=823
        await PromotionService().revalidate_first_visit_booking(session,booking)
        assert (booking.subtotal_amount, booking.promotion_discount_amount, booking.manual_discount_amount,booking.total_amount)==(1200,360,17,823)
        booking.start_at=datetime.now(UTC)+timedelta(days=20)
        with pytest.raises(HTTPException) as exc:
            await PromotionService().revalidate_first_visit_booking(session,booking)
        assert exc.value.status_code==410


@pytest.mark.anyio
async def test_expired_offer_rejected(database):
    ids=await setup_offer(database)
    async with database() as session:
        offer=await session.get(CampaignOffer,ids[-2])
        offer.expires_at=datetime.now(UTC)-timedelta(seconds=1)
        await session.commit()
    with pytest.raises(HTTPException) as exc:
        await reserve(database,ids)
    assert exc.value.status_code==410


@pytest.mark.anyio
async def test_redemption_snapshot_survives_status_and_price_corrections(database):
    ids=await setup_offer(database)
    booking_id=await reserve(database,ids)
    async with database() as session:
        booking=await session.get(Booking,booking_id)
        await PromotionService().sync_first_visit_status(session,booking,BookingStatus.completed)
        booking.status=BookingStatus.completed
        await session.commit()
        offer=await session.get(CampaignOffer,ids[-2])
        completed_at=offer.completed_at
        assert (offer.redeemed_discount_amount,offer.redeemed_revenue_amount)==(240,560)
        await PromotionService().sync_first_visit_status(session,booking,BookingStatus.cancelled)
        booking.status=BookingStatus.cancelled
        booking.promotion_discount_amount=300
        booking.total_amount=900
        await session.commit()
        await PromotionService().sync_first_visit_status(session,booking,BookingStatus.completed)
        booking.status=BookingStatus.completed
        await session.commit()
        assert offer.consumed_booking_id==booking_id
        assert offer.completed_at==completed_at
        assert (offer.redeemed_discount_amount,offer.redeemed_revenue_amount)==(240,560)


@pytest.mark.anyio
async def test_existing_booking_service_persists_offer_attribution_atomically(database):
    from app.schemas.booking import PublicBookingCreate
    from app.services.booking import BookingServiceLayer
    ids = await setup_offer(database)
    customer_id, master_id, service_id, offer_id, token = ids
    start = (datetime.now(UTC)+timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    async with database() as session:
        booking = await BookingServiceLayer().create_public_booking(session,
            PublicBookingCreate(master_id=master_id, service_id=service_id, customer_phone='+380501111111',
                                customer_name='Test', start_at=start, offer_token=token, expected_total_amount=560),
            require_availability=False, require_working_hours=False)
        assert booking.total_amount == 560
        assert booking.customer_id == customer_id
        assert booking.campaign_offer_id == booking.campaign_offer_reservation_id == offer_id
        offer = await session.get(CampaignOffer, offer_id)
        assert (booking.campaign_id, booking.campaign_run_id, booking.campaign_recipient_id) == (offer.campaign_id, offer.run_id, offer.recipient_id)
