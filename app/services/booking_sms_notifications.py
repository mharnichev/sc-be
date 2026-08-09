from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.messaging import Campaign, CampaignStatus, CampaignType, MessageChannel
from app.services.booking import KYIV_TZ
from app.services.sms import SmsService

logger = logging.getLogger(__name__)

SMS_BOOKING_CONFIRMATION_LOCATION_KEY = "sms_booking_confirmation"
SMS_BOOKING_TWO_HOUR_REMINDER_LOCATION_KEY = "sms_booking_two_hour_reminder"


@dataclass(frozen=True)
class BookingSmsNotification:
    booking_id: int
    master_name: str
    customer_name: str
    customer_phone: str
    start_at: datetime
    end_at: datetime


class BookingSmsNotificationService:
    def __init__(self, sms_service: SmsService | None = None) -> None:
        self.sms_service = sms_service or SmsService()

    async def send_booking_confirmation(self, notification: BookingSmsNotification, *, body: str | None = None) -> bool:
        if body is None and not settings.booking_sms_notifications_enabled:
            logger.info("Booking SMS confirmation skipped: disabled", extra={"booking_id": notification.booking_id})
            return False
        body = body or self.build_message(settings.booking_sms_confirmation_template, notification)
        await self.sms_service.send_message(notification.customer_phone, body)
        logger.info("Booking SMS confirmation sent", extra={"booking_id": notification.booking_id})
        return True

    async def send_booking_reminder(self, notification: BookingSmsNotification, *, body: str | None = None) -> bool:
        if body is None and not settings.booking_sms_reminders_enabled:
            logger.info("Booking SMS reminder skipped: disabled", extra={"booking_id": notification.booking_id})
            return False
        body = body or self.build_message(settings.booking_sms_two_hour_reminder_template, notification)
        await self.sms_service.send_message(notification.customer_phone, body)
        logger.info("Booking SMS reminder sent", extra={"booking_id": notification.booking_id})
        return True

    async def booking_confirmation_body(
        self,
        session: AsyncSession,
        notification: BookingSmsNotification,
    ) -> str | None:
        campaign = await self.active_sms_campaign(
            session,
            CampaignType.booking_confirmation,
            location_key=SMS_BOOKING_CONFIRMATION_LOCATION_KEY,
        )
        if campaign is not None:
            template = self.campaign_body(campaign)
            return self.build_message(template, notification) if template else None
        if settings.booking_sms_notifications_enabled:
            return self.build_message(settings.booking_sms_confirmation_template, notification)
        return None

    async def send_due_booking_reminders(self, session: AsyncSession) -> int:
        campaigns = await self.active_sms_reminder_campaigns(session)
        if campaigns:
            sent = 0
            now = datetime.now(KYIV_TZ)
            for campaign in campaigns:
                template = self.campaign_body(campaign)
                if not template:
                    logger.info("Booking SMS reminder campaign skipped: empty body", extra={"campaign_id": campaign.id})
                    continue
                metadata = campaign.metadata_json or {}
                sent += await self._send_due_reminders(
                    session,
                    now=now,
                    lead_hours=max(1, int(metadata.get("lead_hours") or settings.booking_sms_two_hour_reminder_lead_hours)),
                    window_minutes=max(1, int(metadata.get("window_minutes") or settings.booking_sms_two_hour_reminder_window_minutes)),
                    sent_at_field="sms_two_hour_reminder_sent_at",
                    sent_at_column=Booking.sms_two_hour_reminder_sent_at,
                    template=template,
                    label=campaign.location_key or f"campaign:{campaign.id}",
                )
            await session.commit()
            return sent

        if not settings.booking_sms_reminders_enabled:
            logger.info("Booking SMS reminders job skipped: disabled")
            return 0

        now = datetime.now(KYIV_TZ)
        sent = 0
        if settings.booking_sms_two_hour_reminders_enabled:
            sent += await self._send_due_reminders(
                session,
                now=now,
                lead_hours=settings.booking_sms_two_hour_reminder_lead_hours,
                window_minutes=settings.booking_sms_two_hour_reminder_window_minutes,
                sent_at_field="sms_two_hour_reminder_sent_at",
                sent_at_column=Booking.sms_two_hour_reminder_sent_at,
                template=settings.booking_sms_two_hour_reminder_template,
                label="two-hour",
            )
        await session.commit()
        return sent

    async def active_sms_campaign(
        self,
        session: AsyncSession,
        campaign_type: CampaignType,
        *,
        location_key: str | None = None,
    ) -> Campaign | None:
        stmt = (
            select(Campaign)
            .options(selectinload(Campaign.template))
            .where(
                Campaign.channel == MessageChannel.sms,
                Campaign.type == campaign_type,
                Campaign.status == CampaignStatus.active,
            )
            .order_by(Campaign.updated_at.desc(), Campaign.id.desc())
        )
        if location_key is not None:
            stmt = stmt.where(Campaign.location_key == location_key)
        return (await session.execute(stmt.limit(1))).scalar_one_or_none()

    async def active_sms_reminder_campaigns(self, session: AsyncSession) -> list[Campaign]:
        campaigns = (
            (
                await session.execute(
                    select(Campaign)
                    .options(selectinload(Campaign.template))
                    .where(
                        Campaign.channel == MessageChannel.sms,
                        Campaign.type == CampaignType.appointment_reminder,
                        Campaign.status == CampaignStatus.active,
                    )
                    .order_by(Campaign.updated_at.desc(), Campaign.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return [
            campaign
            for campaign in campaigns
            if campaign.location_key == SMS_BOOKING_TWO_HOUR_REMINDER_LOCATION_KEY
            or (campaign.metadata_json or {}).get("trigger") == "booking_upcoming"
        ]

    def campaign_body(self, campaign: Campaign) -> str | None:
        metadata = campaign.metadata_json or {}
        metadata_body = metadata.get("message_body")
        if isinstance(metadata_body, str) and metadata_body.strip():
            return metadata_body
        if campaign.template is not None:
            return campaign.template.body
        return None

    async def _send_due_reminders(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        lead_hours: int,
        window_minutes: int,
        sent_at_field: str,
        sent_at_column: Any,
        template: str,
        label: str,
    ) -> int:
        window_start = now + timedelta(hours=lead_hours)
        window_end = window_start + timedelta(minutes=window_minutes)
        bookings = (
            await session.execute(
                select(Booking)
                .options(
                    selectinload(Booking.master),
                    selectinload(Booking.redirected_from_master),
                )
                .where(
                    Booking.status == BookingStatus.confirmed,
                    sent_at_column.is_(None),
                    Booking.start_at >= window_start,
                    Booking.start_at < window_end,
                )
                .order_by(Booking.start_at.asc())
            )
        ).scalars().all()

        sent = 0
        for booking in bookings:
            try:
                notification = self.notification_from_booking(booking)
                body = self.build_message(template, notification)
                if await self.send_booking_reminder(notification, body=body):
                    setattr(booking, sent_at_field, now)
                    sent += 1
            except Exception:
                logger.exception("Booking SMS reminder failed", extra={"booking_id": booking.id, "reminder": label})

        return sent

    def notification_from_booking(self, booking: Booking) -> BookingSmsNotification:
        master_name = ""
        public_master = getattr(booking, "redirected_from_master", None) or booking.master
        if public_master is not None:
            master_name = getattr(public_master, "full_name_uk", None) or public_master.full_name
        return BookingSmsNotification(
            booking_id=booking.id,
            master_name=master_name,
            customer_name=booking.customer_name,
            customer_phone=booking.customer_phone,
            start_at=booking.start_at,
            end_at=booking.end_at,
        )

    def build_message(self, template: str, notification: BookingSmsNotification) -> str:
        start_at = notification.start_at.astimezone(KYIV_TZ)
        end_at = notification.end_at.astimezone(KYIV_TZ)
        return template.format(
            booking_id=notification.booking_id,
            master_name=notification.master_name,
            customer_name=notification.customer_name,
            customer_phone=notification.customer_phone,
            appointment_date=f"{start_at:%d.%m.%Y}",
            appointment_time=f"{start_at:%H:%M}",
            appointment_end_time=f"{end_at:%H:%M}",
            barbershop_name=settings.barbershop_name,
        )


booking_sms_notification_service = BookingSmsNotificationService()
