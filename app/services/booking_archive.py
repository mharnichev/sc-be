"""Archive a booking and invalidate pending actions in the same transaction."""
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.booking import Booking, BookingStatus
from app.models.customer import Customer
from app.models.messaging import MessageDeliveryStatus, MessageRecipient, ReviewRequest, ReviewRequestStatus
from app.models.repeat_booking import RepeatBookingOffer, RepeatBookingOfferStatus
from app.services.master_reviews import master_review_service
from app.services.promotion import PromotionService
from app.services.repeat_booking import repeat_booking_service


async def archive_booking(session, booking: Booking) -> None:
    customer = await session.get(Customer, booking.customer_id) if booking.customer_id else None
    if customer is not None:
        customer = await PromotionService().lock_customer(session, customer)
    booking.archived_at = datetime.now(UTC)
    # Reservation keys have unique constraints; releasing them permits future visits.
    # The original promotion and campaign attribution snapshots remain intact.
    booking.first_visit_customer_id = None
    booking.campaign_offer_reservation_id = None
    await session.flush()
    if customer is not None:
        customer.first_visit_completed_at = await session.scalar(
            select(func.min(Booking.end_at)).where(
                Booking.customer_id == customer.id,
                Booking.status == BookingStatus.completed,
                Booking.archived_at.is_(None),
            )
        )
    offers = (await session.scalars(select(RepeatBookingOffer).where(
        RepeatBookingOffer.completed_booking_id == booking.id,
        RepeatBookingOffer.status.in_([
            RepeatBookingOfferStatus.scheduled, RepeatBookingOfferStatus.sent,
            RepeatBookingOfferStatus.opened, RepeatBookingOfferStatus.started,
            RepeatBookingOfferStatus.failed,
        ]),
    ).order_by(RepeatBookingOffer.id).with_for_update())).all()
    for offer in offers:
        await repeat_booking_service._skip(session, offer, "source_booking_archived")
    recipients = (await session.scalars(select(MessageRecipient).where(
        MessageRecipient.appointment_id == booking.id,
        MessageRecipient.status == MessageDeliveryStatus.pending,
    ).order_by(MessageRecipient.id).with_for_update())).all()
    for recipient in recipients:
        recipient.status = MessageDeliveryStatus.skipped
        recipient.last_error = "booking_archived"
    requests = (await session.scalars(select(ReviewRequest).where(
        ReviewRequest.appointment_id == booking.id,
    ).options(selectinload(ReviewRequest.events)).order_by(ReviewRequest.id).with_for_update())).all()
    for request in requests:
        request.token_hash = None
        if request.status != ReviewRequestStatus.submitted:
            master_review_service.transition_request(request, ReviewRequestStatus.failed, reason="booking_archived")
