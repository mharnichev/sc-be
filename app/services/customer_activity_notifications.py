from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
import asyncio

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import Booking
from app.models.customer_activity import CustomerActivityAccessToken
from app.models.messaging import (
    Campaign,
    CampaignStatus,
    CampaignType,
    ClientCommunicationPreference,
    MessageChannel,
    MessageDeliveryStatus,
    MessageLog,
    MessagePurpose,
    MessageRecipient,
)
from app.models.waitlist import WaitlistRequest
from app.services.booking_sms_notifications import (
    BOOKING_CANCEL_URL_VARIABLE,
    BOOKING_MANAGE_URL_VARIABLE,
    DEFAULT_CUSTOMER_ACTIVITY_BOOKING_CONFIRMATION_BODY,
    SMS_BOOKING_CONFIRMATION_LOCATION_KEY,
    booking_sms_notification_service,
)
from app.services.customer_activity import customer_activity_service
from app.services.messaging import MessagingService
from app.services.sms import SmsService


logger = logging.getLogger(__name__)
WAITLIST_CREATED_LOCATION_KEY = "sms_waitlist_created"


class CustomerActivityNotificationService:
    """Transactional SMS with auditable delivery and no stored raw capability."""

    def __init__(self, sms_service: SmsService | None = None) -> None:
        self.sms_service = sms_service or SmsService()
        self.messaging_service = MessagingService()

    async def send_booking_confirmation(self, booking_id: int) -> bool:
        async with AsyncSessionLocal() as session:
            booking = (
                await session.execute(
                    select(Booking)
                    .options(
                        selectinload(Booking.customer),
                        selectinload(Booking.master),
                        selectinload(Booking.redirected_from_master),
                    )
                    .where(Booking.id == booking_id)
                )
            ).scalar_one_or_none()
            if booking is None or booking.customer is None:
                return False
            notification = booking_sms_notification_service.notification_from_booking(booking)
            campaign = await booking_sms_notification_service.active_sms_campaign(
                session, CampaignType.booking_confirmation, location_key=SMS_BOOKING_CONFIRMATION_LOCATION_KEY
            )
            if campaign is None:
                if not settings.booking_sms_notifications_enabled:
                    return False
                campaign = await self._system_booking_campaign(session)
            template = booking_sms_notification_service.campaign_body(campaign)
            if not template:
                return False
            self.messaging_service.validate_booking_confirmation_template_body(template)
            body = booking_sms_notification_service.build_message(
                template,
                notification,
                manage_url=BOOKING_MANAGE_URL_VARIABLE,
                cancel_url=BOOKING_CANCEL_URL_VARIABLE,
            )
            recipient = await self._enqueue(
                session,
                campaign=campaign,
                customer=booking.customer,
                body=body,
                source="booking_confirmation",
                booking=booking,
            )
            await session.commit()
            if recipient is None:
                return False
            return await self._dispatch(recipient)

    async def send_waitlist_created(self, request_id: int) -> bool:
        async with AsyncSessionLocal() as session:
            request = (
                await session.execute(
                    select(WaitlistRequest)
                    .options(selectinload(WaitlistRequest.customer), selectinload(WaitlistRequest.preferred_master))
                    .where(WaitlistRequest.id == request_id)
                )
            ).scalar_one_or_none()
            if request is None or request.customer is None or not request.notification_consent:
                return False
            campaign = await self._campaign(session, WAITLIST_CREATED_LOCATION_KEY)
            if campaign is None:
                logger.error("Waitlist created SMS campaign is missing")
                return False
            master = (
                request.preferred_master.full_name_uk
                if request.preferred_master is not None
                else "будь-якого доступного майстра"
            )
            body = (
                f"Ви додані до листа очікування для {master} на {request.desired_date:%d.%m.%Y}. "
                "Керувати заявкою:\n"
                f"Переглянути: {BOOKING_MANAGE_URL_VARIABLE}\n"
                f"Скасувати: {BOOKING_CANCEL_URL_VARIABLE}"
            )
            scheduled_at = MessagingService.adjust_for_quiet_hours(
                datetime.now(UTC),
                quiet_from=settings.waitlist_quiet_hours_from,
                quiet_to=settings.waitlist_quiet_hours_to,
            )
            recipient = await self._enqueue(
                session,
                campaign=campaign,
                customer=request.customer,
                body=body,
                source="waitlist_created",
                request=request,
                scheduled_at=scheduled_at,
            )
            await session.commit()
            if recipient is None or scheduled_at > datetime.now(UTC):
                return recipient is not None
            return await self._dispatch(recipient)

    async def dispatch_due_waitlist_created(self, limit: int = 50) -> int:
        async with AsyncSessionLocal() as session:
            rows = list(
                (
                    await session.execute(
                        select(MessageRecipient.id)
                        .join(Campaign)
                        .where(
                            or_(
                                and_(
                                    Campaign.location_key == WAITLIST_CREATED_LOCATION_KEY,
                                    MessageRecipient.idempotency_key.like(
                                        "customer-activity:waitlist_created:waitlist:%"
                                    ),
                                ),
                                and_(
                                    Campaign.location_key == SMS_BOOKING_CONFIRMATION_LOCATION_KEY,
                                    MessageRecipient.idempotency_key.like(
                                        "customer-activity:booking_confirmation:booking:%"
                                    ),
                                ),
                            ),
                            MessageRecipient.status == MessageDeliveryStatus.pending,
                            or_(MessageRecipient.scheduled_at.is_(None), MessageRecipient.scheduled_at <= datetime.now(UTC)),
                            or_(MessageRecipient.next_retry_at.is_(None), MessageRecipient.next_retry_at <= datetime.now(UTC)),
                        )
                        .limit(limit)
                    )
                ).scalars()
            )
        return sum([await self._dispatch(item) for item in rows])

    async def _campaign(self, session: AsyncSession, location_key: str) -> Campaign | None:
        return (
            await session.execute(
                select(Campaign).where(
                    Campaign.location_key == location_key,
                    Campaign.channel == MessageChannel.sms,
                    Campaign.status == CampaignStatus.active,
                ).order_by(Campaign.updated_at.desc(), Campaign.id.desc()).limit(1)
            )
        ).scalar_one_or_none()

    async def _system_booking_campaign(self, session: AsyncSession) -> Campaign:
        """Fallback audit container when legacy config enabled SMS without seed."""
        campaign = await self._campaign(session, SMS_BOOKING_CONFIRMATION_LOCATION_KEY)
        if campaign is not None:
            return campaign
        campaign = Campaign(
            name="System SMS booking confirmation",
            type=CampaignType.booking_confirmation,
            status=CampaignStatus.active,
            channel=MessageChannel.sms,
            purpose=MessagePurpose.transactional,
            timezone="Europe/Kyiv",
            location_key=SMS_BOOKING_CONFIRMATION_LOCATION_KEY,
            metadata_json={
                "system": "customer_activity_fallback",
                "message_body": DEFAULT_CUSTOMER_ACTIVITY_BOOKING_CONFIRMATION_BODY,
            },
        )
        session.add(campaign)
        await session.flush()
        return campaign

    async def _enqueue(
        self,
        session: AsyncSession,
        *,
        campaign: Campaign,
        customer,
        body: str,
        source: str,
        booking: Booking | None = None,
        request: WaitlistRequest | None = None,
        scheduled_at: datetime | None = None,
    ) -> int | None:
        target = f"booking:{booking.id}" if booking is not None else f"waitlist:{request.id if request else 'none'}"
        key = f"customer-activity:{source}:{target}"
        existing = (
            await session.execute(select(MessageRecipient.id).where(MessageRecipient.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is not None:
            return None
        preference = await self.messaging_service.get_preference(session, customer.id)
        allowed, reason = self.messaging_service.communication_allowed(preference, MessagePurpose.transactional)
        recipient = MessageRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            appointment_id=booking.id if booking else None,
            waitlist_request_id=request.id if request else None,
            channel=MessageChannel.sms,
            status=MessageDeliveryStatus.pending if allowed else MessageDeliveryStatus.skipped,
            scheduled_at=scheduled_at,
            idempotency_key=key,
            # Backoffice gets the complete rendered copy with safe URL variables,
            # while opaque capability values are created only at delivery time.
            rendered_message=body,
            last_error=reason,
        )
        session.add(recipient)
        await session.flush()
        if not allowed:
            session.add(self._log(recipient, MessageDeliveryStatus.skipped, error_reason=reason))
        return recipient.id

    async def _dispatch(self, recipient_id: int) -> bool:
        async with AsyncSessionLocal() as session:
            recipient = (
                await session.execute(
                    select(MessageRecipient)
                    .options(
                        selectinload(MessageRecipient.customer),
                        selectinload(MessageRecipient.appointment),
                        selectinload(MessageRecipient.waitlist_request),
                    )
                    .where(MessageRecipient.id == recipient_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if recipient is None or recipient.status != MessageDeliveryStatus.pending:
                return False
            if not recipient.idempotency_key.startswith("customer-activity:"):
                return False
            if recipient.appointment_id is None and recipient.waitlist_request_id is None:
                return False
            source = "booking_confirmation" if recipient.appointment_id else "waitlist_created"
            expires_at = self._token_expiry(recipient)
            token = await customer_activity_service.create_access_token(
                session,
                recipient.customer_id,
                source=source,
                expires_at=expires_at,
                source_booking_id=recipient.appointment_id,
                source_waitlist_request_id=recipient.waitlist_request_id,
                recipient_id=recipient.id,
            )
            manage_url, cancel_url = customer_activity_service.urls_for_token(token)
            body = self._render_activity_message(
                recipient.rendered_message,
                manage_url=manage_url,
                cancel_url=cancel_url,
            )
            recipient.attempts += 1
            try:
                result = await self.sms_service.send_message(recipient.customer.phone, body, sensitive=True)
            except Exception as exc:
                await session.execute(
                    update(CustomerActivityAccessToken)
                    .where(
                        CustomerActivityAccessToken.recipient_id == recipient.id,
                        CustomerActivityAccessToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(UTC))
                )
                recipient.last_error = str(exc)
                if recipient.attempts >= settings.messaging_max_retry_attempts:
                    recipient.status = MessageDeliveryStatus.failed
                else:
                    recipient.next_retry_at = datetime.now(UTC) + timedelta(minutes=settings.messaging_retry_delay_minutes)
                session.add(self._log(recipient, MessageDeliveryStatus.failed, error_reason=recipient.last_error))
                await session.commit()
                logger.exception("Customer activity SMS failed", extra={"recipient_id": recipient.id})
                return False
            recipient.status = MessageDeliveryStatus.sent
            recipient.sent_at = datetime.now(UTC)
            recipient.provider_message_id = result.provider_message_id
            recipient.last_error = None
            session.add(self._log(recipient, MessageDeliveryStatus.sent, provider_response={"accepted": True, "provider_message_id": result.provider_message_id}))
            await session.commit()
            return True

    def _render_activity_message(
        self,
        body: str,
        *,
        manage_url: str,
        cancel_url: str,
    ) -> str:
        return self.messaging_service.render_template(
            body,
            {
                "manage_url": manage_url,
                "cancel_url": cancel_url,
            },
        )

    @staticmethod
    def _token_expiry(recipient: MessageRecipient) -> datetime:
        now = datetime.now(UTC)
        cap = now + timedelta(days=settings.customer_activity_token_max_days)
        if recipient.appointment is not None:
            return min(cap, max(now + timedelta(days=settings.customer_activity_token_ttl_days), recipient.appointment.start_at + timedelta(days=1)))
        if recipient.waitlist_request is not None:
            return min(cap, max(now + timedelta(days=settings.customer_activity_token_ttl_days), recipient.waitlist_request.expires_at))
        return min(cap, now + timedelta(days=settings.customer_activity_token_ttl_days))

    @staticmethod
    def _log(
        recipient: MessageRecipient,
        state: MessageDeliveryStatus,
        *,
        provider_response: dict | None = None,
        error_reason: str | None = None,
    ) -> MessageLog:
        return MessageLog(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            customer_id=recipient.customer_id,
            appointment_id=recipient.appointment_id,
            waitlist_request_id=recipient.waitlist_request_id,
            waitlist_offer_id=recipient.waitlist_offer_id,
            channel=recipient.channel,
            status=state,
            provider_response=provider_response,
            error_reason=error_reason,
        )


customer_activity_notification_service = CustomerActivityNotificationService()


async def run_customer_activity_notification_scheduler() -> None:
    """Delivers waitlist-created notifications deferred by quiet hours."""
    while True:
        try:
            await customer_activity_notification_service.dispatch_due_waitlist_created()
        except Exception:
            logger.exception("Customer activity notification scheduler failed")
        await asyncio.sleep(60)
