from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.services.booking import KYIV_TZ
from app.services.sms import SmsService

logger = logging.getLogger(__name__)


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

    async def send_booking_confirmation(self, notification: BookingSmsNotification) -> bool:
        if not settings.booking_sms_notifications_enabled:
            logger.info("Booking SMS confirmation skipped: disabled", extra={"booking_id": notification.booking_id})
            return False
        body = self.build_message(settings.booking_sms_confirmation_template, notification)
        await self.sms_service.send_message(notification.customer_phone, body)
        logger.info("Booking SMS confirmation sent", extra={"booking_id": notification.booking_id})
        return True

    async def send_booking_reminder(self, notification: BookingSmsNotification) -> bool:
        if not settings.booking_sms_reminders_enabled:
            logger.info("Booking SMS reminder skipped: disabled", extra={"booking_id": notification.booking_id})
            return False
        body = self.build_message(settings.booking_sms_reminder_template, notification)
        await self.sms_service.send_message(notification.customer_phone, body)
        logger.info("Booking SMS reminder sent", extra={"booking_id": notification.booking_id})
        return True

    async def send_due_booking_reminders(self, session: AsyncSession) -> int:
        if not settings.booking_sms_reminders_enabled:
            logger.info("Booking SMS reminders job skipped: disabled")
            return 0

        now = datetime.now(KYIV_TZ)
        window_start = now + timedelta(hours=settings.booking_sms_reminder_lead_hours)
        window_end = window_start + timedelta(minutes=settings.booking_sms_reminder_window_minutes)
        bookings = (
            await session.execute(
                select(Booking)
                .options(selectinload(Booking.master))
                .where(
                    Booking.status == BookingStatus.confirmed,
                    Booking.sms_reminder_sent_at.is_(None),
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
                if await self.send_booking_reminder(notification):
                    booking.sms_reminder_sent_at = now
                    sent += 1
            except Exception:
                logger.exception("Booking SMS reminder failed", extra={"booking_id": booking.id})

        await session.commit()
        return sent

    def notification_from_booking(self, booking: Booking) -> BookingSmsNotification:
        master_name = ""
        if booking.master is not None:
            master_name = getattr(booking.master, "full_name_uk", None) or booking.master.full_name
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
