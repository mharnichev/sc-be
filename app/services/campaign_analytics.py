"""Aggregate each source separately to avoid recipient/event/booking join inflation."""
from sqlalchemy import case, func, select
from app.models.booking import Booking, BookingStatus
from app.models.campaign_offer import CampaignOffer
from app.models.campaign_run import CampaignRun
from app.models.messaging import MessageDeliveryStatus, MessageRecipient


async def offer_analytics(session, *, campaign_id=None, run_id=None, master_id=None, promotion_id=None, date_from=None, date_to=None):
    predicates = [column == value for column, value in (
        (CampaignOffer.campaign_id, campaign_id), (CampaignOffer.run_id, run_id),
        (CampaignOffer.master_id, master_id), (CampaignOffer.promotion_id, promotion_id)) if value is not None]
    cohort_at = func.coalesce(CampaignRun.evaluated_at, CampaignRun.created_at)
    date_predicates = []
    if date_from is not None:
        date_predicates.append(cohort_at >= date_from)
    if date_to is not None:
        date_predicates.append(cohort_at < date_to)
    if date_predicates:
        predicates.append(CampaignOffer.run_id.in_(select(CampaignRun.id).where(*date_predicates)))
    offers = select(CampaignOffer.id).where(*predicates)
    recipients = select(CampaignOffer.recipient_id).where(*predicates)
    runs = select(CampaignOffer.run_id).where(*predicates).distinct()
    run_predicates = [*date_predicates, CampaignRun.campaign_snapshot["offer_master_id"].as_integer().is_not(None)]
    if campaign_id is not None:
        run_predicates.append(CampaignRun.campaign_id == campaign_id)
    if run_id is not None:
        run_predicates.append(CampaignRun.id == run_id)
    if master_id is not None or promotion_id is not None:
        run_predicates.append(CampaignRun.id.in_(runs))
    audience = await session.scalar(select(func.coalesce(func.sum(CampaignRun.audience_count), 0)).where(*run_predicates))
    # Snapshot communication eligibility is a persisted fact; sending outcomes are separate.
    eligible = await session.scalar(select(func.count()).select_from(CampaignOffer).where(*predicates,
        CampaignOffer.conditions_snapshot["communication_eligible"].as_boolean().is_(True)))
    accepted = await session.scalar(select(func.count()).select_from(MessageRecipient).where(
        MessageRecipient.id.in_(recipients), MessageRecipient.sent_at.is_not(None)))
    counts = dict((await session.execute(select(MessageRecipient.status, func.count()).where(
        MessageRecipient.id.in_(recipients)).group_by(MessageRecipient.status))).all())
    raw, confirmed, openers = (await session.execute(select(
        func.coalesce(func.sum(CampaignOffer.raw_link_requests), 0),
        func.coalesce(func.sum(CampaignOffer.confirmed_open_count), 0),
        func.count(func.distinct(case((CampaignOffer.first_confirmed_open_at.is_not(None), CampaignOffer.customer_id)))))
        .where(*predicates))).one()
    booked = Booking.campaign_offer_id.in_(offers)
    booking_counts = dict((await session.execute(select(Booking.status, func.count()).where(booked).group_by(Booking.status))).all())
    completed, redeemed, discount, revenue = (await session.execute(select(
        func.count(CampaignOffer.consumed_booking_id), func.count(func.distinct(CampaignOffer.customer_id)),
        func.coalesce(func.sum(CampaignOffer.redeemed_discount_amount), 0),
        func.coalesce(func.sum(CampaignOffer.redeemed_revenue_amount), 0)).join(
            Booking, Booking.id == CampaignOffer.consumed_booking_id).where(
            *predicates, CampaignOffer.consumed_booking_id.is_not(None)))).one()
    bookings = sum(booking_counts.values())
    return dict(date_from=date_from, date_to=date_to, period_basis="run_cohort", campaign_id=campaign_id, run_id=run_id, master_id=master_id, promotion_id=promotion_id,
                audience_size=audience, communication_eligible_recipients=eligible,
                provider_accepted=accepted,
                delivered=counts.get(MessageDeliveryStatus.delivered, 0), raw_link_requests=raw,
                confirmed_page_opens=confirmed, unique_confirmed_openers=openers, bookings_created=bookings,
                cancelled_bookings=booking_counts.get(BookingStatus.cancelled, 0), no_show=booking_counts.get(BookingStatus.no_show, 0),
                completed_visits=completed, unique_customers_redeemed=redeemed,
                total_discount_amount=discount, observed_completed_revenue=revenue,
                booking_conversion_percent=bookings/eligible*100 if eligible else None,
                redemption_conversion_percent=redeemed/eligible*100 if eligible else None)
