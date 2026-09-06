"""Durable waitlist SMS projections; provider I/O stays in the shared queue."""
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.customer import Customer
from app.models.booking_recovery import BookingRecoveryEventType
from app.services.booking_recovery_analytics import booking_recovery_analytics_service
from app.models.messaging import ClientCommunicationPreference, MessageDeliveryStatus, MessageLog, MessageRecipient
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.services.sms_queue import SmsDispatchDecision


async def sms_job_eligibility(job):
    from app.services.waitlist_offers import waitlist_offer_service
    async with AsyncSessionLocal() as session:
        offer = await session.get(WaitlistOffer, job.context_json["waitlist_offer_id"])
        now = datetime.now(UTC)
        if offer is None or offer.status != WaitlistOfferStatus.pending:
            return SmsDispatchDecision("skip", "waitlist_offer_inactive")
        if offer.expires_at <= now or offer.start_at <= now:
            return SmsDispatchDecision("skip", "waitlist_offer_expired")
        request = await session.get(WaitlistRequest, offer.request_id)
        if request is None or request.status not in {WaitlistStatus.active, WaitlistStatus.offered}:
            return SmsDispatchDecision("skip", "waitlist_request_inactive")
        customer = await session.get(Customer, request.customer_id)
        preference = await session.scalar(select(ClientCommunicationPreference).where(
            ClientCommunicationPreference.customer_id == request.customer_id,
        ))
        if customer is None or not customer.is_active or not waitlist_offer_service._communication_allowed(request, preference):
            return SmsDispatchDecision("skip", "waitlist_contact_restricted")
        return SmsDispatchDecision()


async def sms_job_outcome(job):
    from app.services.waitlist_offers import waitlist_offer_service
    async with AsyncSessionLocal() as session:
        offer = await session.get(WaitlistOffer, job.context_json["waitlist_offer_id"], with_for_update=True)
        if offer is None:
            return
        request = await session.get(WaitlistRequest, offer.request_id)
        if request is None:
            return
        if offer.status == WaitlistOfferStatus.delivered and job.status != "delivered":
            return
        if job.status in {"accepted", "delivered"}:
            if offer.status not in {WaitlistOfferStatus.pending, WaitlistOfferStatus.sent, WaitlistOfferStatus.delivered}:
                return
            offer.status = WaitlistOfferStatus.delivered if job.status == "delivered" else WaitlistOfferStatus.sent
            offer.sent_at = offer.sent_at or job.transport_started_at or datetime.now(UTC)
            offer.provider_message_id = job.provider_message_id
            if job.status == "delivered":
                offer.delivered_at = offer.delivered_at or job.delivered_at or datetime.now(UTC)
            request.status = WaitlistStatus.offered
            request.offered_at = request.offered_at or offer.sent_at
            await waitlist_offer_service._record_offer_message(
                session, offer=offer, request=request,
                body=job.context_json.get("safe_body", "Waitlist offer"),
                booking_link="__QUEUE_REDACTED_LINK__", provider_message_id=job.provider_message_id,
            )
        elif job.status in {"failed", "skipped", "cancelled"} and offer.status in {WaitlistOfferStatus.pending, WaitlistOfferStatus.sent}:
            offer.status = WaitlistOfferStatus.cancelled
            offer.closed_at = datetime.now(UTC)
            offer.close_reason = job.error_code or "sms_send_failed"
            if request.status == WaitlistStatus.offered:
                request.status = WaitlistStatus.active
                request.offered_at = None
        elif job.status == "uncertain" and offer.status == WaitlistOfferStatus.pending:
            # Keep the existing hold until expiry: acceptance may have occurred.
            offer.close_reason = "sms_delivery_uncertain"
        recipient = await session.scalar(select(MessageRecipient).where(
            MessageRecipient.waitlist_offer_id == offer.id,
        ).with_for_update())
        if recipient is not None:
            recipient.sms_queue_job_id = job.id
            desired = {"accepted": MessageDeliveryStatus.sent,
                       "delivered": MessageDeliveryStatus.delivered,
                       "failed": MessageDeliveryStatus.failed}.get(job.status)
            if desired and recipient.status != desired and recipient.status != MessageDeliveryStatus.delivered:
                recipient.status = desired
                recipient.delivered_at = offer.delivered_at
                recipient.last_error = job.error_code if desired == MessageDeliveryStatus.failed else None
                recipient.delivery_status_checked_at = datetime.now(UTC)
                session.add(MessageLog(
                    campaign_id=recipient.campaign_id, recipient_id=recipient.id,
                    customer_id=request.customer_id, waitlist_request_id=request.id,
                    waitlist_offer_id=offer.id, channel=recipient.channel, status=desired,
                    provider_response={"provider_message_id": job.provider_message_id},
                    error_reason=recipient.last_error,
                ))
        events = []
        if job.status in {"accepted", "delivered"} and offer.sent_at:
            events.append((BookingRecoveryEventType.waitlist_offer_sent, "waitlist-offer-sent", offer.sent_at))
        if job.status == "delivered" and offer.delivered_at:
            events.append((BookingRecoveryEventType.waitlist_offer_delivered, "waitlist-offer-delivered", offer.delivered_at))
        if job.error_code == "provider_delivery_failed" and offer.status == WaitlistOfferStatus.cancelled:
            events.append((BookingRecoveryEventType.waitlist_offer_expired, "waitlist-offer-delivery-failed", offer.closed_at))
        for event_type, key, occurred_at in events:
            await booking_recovery_analytics_service.record(
                session, event_type=event_type, event_key=f"{key}:{offer.id}",
                master_id=offer.master_id, waitlist_request_id=request.id,
                waitlist_offer_id=offer.id, source_booking_id=offer.source_booking_id,
                occurred_at=occurred_at,
            )
        await session.commit()
