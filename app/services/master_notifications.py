from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.core.config import settings
from app.models.booking import Booking
from app.services.booking import KYIV_TZ
from app.services.messaging import TelegramMessageProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewBookingTelegram:
    booking_id: int
    master_name: str
    telegram_chat_id: str | None
    service_name: str
    customer_name: str
    customer_phone: str
    customer_comment: str | None
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class CancelledBookingTelegram:
    booking_id: int
    telegram_chat_id: str | None
    service_name: str
    customer_name: str
    start_at: datetime


def cancelled_booking_telegram(booking: Booking) -> CancelledBookingTelegram:
    master = getattr(booking, "redirected_from_master", None) or getattr(booking, "master", None)
    services = list(getattr(booking, "services", []) or [])
    if not services and getattr(booking, "service", None) is not None:
        services = [booking.service]
    service_name = ", ".join(
        getattr(item, "title_uk", None) or getattr(item, "name", "")
        for item in services
    )
    return CancelledBookingTelegram(
        booking_id=booking.id,
        telegram_chat_id=getattr(master, "telegram_chat_id", None),
        service_name=service_name,
        customer_name=booking.customer_name,
        start_at=booking.start_at,
    )


class MasterTelegramNotificationService:
    def __init__(self, provider: TelegramMessageProvider | None = None) -> None:
        self.provider = provider or TelegramMessageProvider()

    async def send_new_booking_to_master(self, notification: NewBookingTelegram) -> None:
        if not notification.telegram_chat_id:
            logger.info(
                "Booking Telegram notification skipped: master has no Telegram chat id",
                extra={"booking_id": notification.booking_id},
            )
            return
        if not settings.telegram_bot_token:
            logger.info(
                "Booking Telegram notification skipped: Telegram bot token is not configured",
                extra={"booking_id": notification.booking_id, "telegram_chat_id": notification.telegram_chat_id},
            )
            return

        body = self.build_new_booking_message(notification)
        await self.provider.send_message(destination=notification.telegram_chat_id, body=body)

    def build_new_booking_message(self, notification: NewBookingTelegram) -> str:
        start_at = notification.start_at.astimezone(KYIV_TZ)
        return f"Йоу! Є нова праця, збирай раму! {notification.customer_name} {notification.service_name} {start_at:%d.%m.%Y %H:%M}"

    async def send_cancelled_booking_to_master(self, notification: CancelledBookingTelegram) -> None:
        if not notification.telegram_chat_id:
            logger.info(
                "Booking cancellation Telegram notification skipped: master has no Telegram chat id",
                extra={"booking_id": notification.booking_id},
            )
            return
        if not settings.telegram_bot_token:
            logger.info(
                "Booking cancellation Telegram notification skipped: Telegram bot token is not configured",
                extra={"booking_id": notification.booking_id, "telegram_chat_id": notification.telegram_chat_id},
            )
            return

        body = self.build_cancelled_booking_message(notification)
        await self.provider.send_message(destination=notification.telegram_chat_id, body=body)

    def build_cancelled_booking_message(self, notification: CancelledBookingTelegram) -> str:
        start_at = notification.start_at.astimezone(KYIV_TZ)
        return (
            f"❗ Клієнт {notification.customer_name} скасував запис: "
            f"{notification.service_name} {start_at:%d.%m.%Y %H:%M}"
        )


master_telegram_notification_service = MasterTelegramNotificationService()
