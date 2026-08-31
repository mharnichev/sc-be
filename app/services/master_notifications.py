from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import Booking
from app.models.messaging import (
    Campaign,
    CampaignStatus,
    CampaignType,
    MasterMessageDelivery,
    MessageChannel,
    MessageDeliveryStatus,
)
from app.services.booking import KYIV_TZ
from app.services.messaging import MessagingService, TelegramMessageProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewBookingTelegram:
    booking_id: int
    master_id: int | None
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
    master_id: int | None
    master_name: str
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
        master_id=getattr(master, "id", None),
        master_name=(
            getattr(master, "full_name_uk", None)
            or getattr(master, "full_name", "")
        ),
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


class MasterCampaignNotificationService:
    """Sends booking lifecycle notifications from editable master campaigns."""

    def __init__(
        self,
        telegram_provider: TelegramMessageProvider | None = None,
        legacy_service: MasterTelegramNotificationService | None = None,
    ) -> None:
        self.telegram_provider = telegram_provider or TelegramMessageProvider()
        self.messaging = MessagingService()
        self.legacy_service = legacy_service or MasterTelegramNotificationService(self.telegram_provider)

    async def send_new_booking_to_master(self, notification: NewBookingTelegram) -> None:
        async with AsyncSessionLocal() as session:
            sent = await self._send(
                session,
                notification,
                campaign_type=CampaignType.master_booking_created,
                trigger="booking_created",
            )
        if not sent:
            await self.legacy_service.send_new_booking_to_master(notification)

    async def send_cancelled_booking_to_master(self, notification: CancelledBookingTelegram) -> None:
        async with AsyncSessionLocal() as session:
            sent = await self._send(
                session,
                notification,
                campaign_type=CampaignType.master_booking_cancelled,
                trigger="booking_cancelled",
            )
        if not sent:
            await self.legacy_service.send_cancelled_booking_to_master(notification)

    async def _send(
        self,
        session: AsyncSession,
        notification: NewBookingTelegram | CancelledBookingTelegram,
        *,
        campaign_type: CampaignType,
        trigger: str,
    ) -> bool:
        if notification.master_id is None:
            logger.warning(
                "Master campaign notification cannot be tracked without master id",
                extra={"booking_id": notification.booking_id, "trigger": trigger},
            )
            return False
        campaign = (
            await session.execute(
                select(Campaign)
                .options(selectinload(Campaign.template))
                .where(Campaign.type == campaign_type)
                .order_by(Campaign.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if campaign is None:
            logger.warning(
                "Active master lifecycle campaign not found; using legacy Telegram copy",
                extra={"booking_id": notification.booking_id, "trigger": trigger},
            )
            return False
        if campaign.status != CampaignStatus.active:
            logger.info(
                "Master lifecycle campaign is not active; notification skipped",
                extra={"campaign_id": campaign.id, "booking_id": notification.booking_id},
            )
            return True

        idempotency_key = f"master:{trigger}:booking:{notification.booking_id}"
        delivery = (
            await session.execute(
                select(MasterMessageDelivery).where(
                    MasterMessageDelivery.idempotency_key == idempotency_key
                )
            )
        ).scalar_one_or_none()
        if delivery is not None and delivery.status in {
            MessageDeliveryStatus.sent,
            MessageDeliveryStatus.delivered,
        }:
            return True

        body = self.messaging.campaign_message_body(campaign)
        if not body:
            logger.error(
                "Master lifecycle campaign has no message body",
                extra={"campaign_id": campaign.id, "booking_id": notification.booking_id},
            )
            return False
        start_at = notification.start_at.astimezone(KYIV_TZ)
        variables = {
            "master_name": notification.master_name,
            "customer_name": notification.customer_name,
            "client_name": notification.customer_name,
            "client": notification.customer_name,
            "service_name": notification.service_name,
            "service": notification.service_name,
            "appointment_date": start_at.strftime("%d.%m.%Y"),
            "appointment_time": start_at.strftime("%H:%M"),
            "date": start_at.strftime("%d.%m.%Y %H:%M"),
        }
        rendered_message = self.messaging.render_template(body, variables)
        if delivery is None:
            delivery = MasterMessageDelivery(
                campaign_id=campaign.id,
                master_id=notification.master_id,
                booking_id=notification.booking_id,
                trigger=trigger,
                channel=campaign.channel,
                status=MessageDeliveryStatus.pending,
                idempotency_key=idempotency_key,
            )
            session.add(delivery)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "Duplicate master lifecycle notification suppressed",
                    extra={"booking_id": notification.booking_id, "trigger": trigger},
                )
                return True
        delivery.rendered_message = rendered_message
        delivery.attempts = (delivery.attempts or 0) + 1

        try:
            channel, provider_message_id = await self._deliver(campaign, notification, rendered_message)
        except Exception as exc:
            delivery.status = MessageDeliveryStatus.failed
            delivery.last_error = str(exc)
            await session.commit()
            logger.exception(
                "Master lifecycle notification failed",
                extra={"campaign_id": campaign.id, "booking_id": notification.booking_id},
            )
            return True

        delivery.channel = channel
        delivery.status = MessageDeliveryStatus.sent
        delivery.provider_message_id = provider_message_id
        delivery.sent_at = datetime.now(UTC)
        delivery.last_error = None
        await session.commit()
        return True

    async def _deliver(
        self,
        campaign: Campaign,
        notification: NewBookingTelegram | CancelledBookingTelegram,
        body: str,
    ) -> tuple[MessageChannel, str | None]:
        if campaign.channel != MessageChannel.telegram:
            raise RuntimeError("Master lifecycle notifications support Telegram only")
        if not notification.telegram_chat_id:
            raise RuntimeError("Master has no Telegram chat id")
        if not settings.telegram_bot_token:
            raise RuntimeError("Telegram bot token is not configured")
        result = await self.telegram_provider.send_message(
            destination=notification.telegram_chat_id,
            body=body,
        )
        return MessageChannel.telegram, result.provider_message_id


master_telegram_notification_service = MasterCampaignNotificationService()
