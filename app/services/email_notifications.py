from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import settings
from app.services.booking import KYIV_TZ

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewBookingEmail:
    booking_id: int
    master_name: str
    master_email: str | None
    service_name: str
    customer_name: str
    customer_phone: str
    customer_comment: str | None
    start_at: datetime
    end_at: datetime


class EmailNotificationService:
    async def send_new_booking_to_master(self, notification: NewBookingEmail) -> None:
        if not notification.master_email:
            logger.info("Booking email notification skipped: master has no email", extra={"booking_id": notification.booking_id})
            return
        if not settings.email_notifications_enabled:
            logger.info(
                "Booking email notification skipped: email notifications are disabled",
                extra={"booking_id": notification.booking_id, "master_email": notification.master_email},
            )
            return

        message = self.build_new_booking_message(notification)
        await asyncio.to_thread(self._send_message, message)

    def build_new_booking_message(self, notification: NewBookingEmail) -> EmailMessage:
        if not settings.smtp_from_email:
            raise RuntimeError("SMTP_FROM_EMAIL is required for email notifications")

        start_at = notification.start_at.astimezone(KYIV_TZ)
        end_at = notification.end_at.astimezone(KYIV_TZ)
        comment = notification.customer_comment or "-"
        subject = f"Нова запис #{notification.booking_id}"
        body = (
            "Нова запис\n\n"
            f"Майстер: {notification.master_name}\n"
            f"Послуга: {notification.service_name}\n"
            f"Час: {start_at:%d.%m.%Y %H:%M} - {end_at:%H:%M}\n"
            f"Клієнт: {notification.customer_name}\n"
            f"Телефон: {notification.customer_phone}\n"
            f"Коментар: {comment}\n"
        )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
        message["To"] = notification.master_email
        message.set_content(body)
        return message

    def _send_message(self, message: EmailMessage) -> None:
        if not settings.smtp_host:
            raise RuntimeError("SMTP_HOST is required for email notifications")

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)


email_notification_service = EmailNotificationService()
