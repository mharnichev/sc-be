from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.core.config import settings
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


master_telegram_notification_service = MasterTelegramNotificationService()
