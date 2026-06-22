from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib import error, request
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.booking import Booking, BookingServiceItem, BookingStatus
from app.models.customer import Customer
from app.models.messaging import (
    Campaign,
    CampaignAudienceFilter,
    CampaignStatus,
    CampaignType,
    ClientCommunicationPreference,
    ConsentStatus,
    MessageChannel,
    MessageDeliveryStatus,
    MessageLog,
    MessagePurpose,
    MessageRecipient,
    MessageTemplate,
    ReviewRequest,
    ReviewPlatform,
)
from app.schemas.messaging import AudienceCriteria
from app.services.sms import SmsService

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")

ALLOWED_TEMPLATE_VARIABLES = {
    "client",
    "client_name",
    "customer_name",
    "barber_name",
    "master_name",
    "date",
    "appointment_date",
    "appointment_time",
    "appointment_end_time",
    "service",
    "service_name",
    "barbershop_name",
    "review_link",
    "discount_code",
}
VARIABLE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
HASH_VARIABLE_PATTERN = re.compile(r"(?<![\w/])#([a-zA-Z_][a-zA-Z0-9_]*)\b")
BRACE_VARIABLE_PATTERN = re.compile(r"(?<!{){\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}(?!})")
MARKETING_PURPOSES = {MessagePurpose.marketing, MessagePurpose.review_request}


@dataclass(frozen=True)
class ProviderSendResult:
    provider_message_id: str | None
    raw_response: dict[str, Any]


class MessageProvider(ABC):
    channel: MessageChannel

    @abstractmethod
    async def send_message(
        self,
        *,
        destination: str,
        body: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> ProviderSendResult:
        raise NotImplementedError


class TelegramMessageProvider(MessageProvider):
    channel = MessageChannel.telegram

    async def answer_callback_query(self, *, callback_query_id: str, text: str | None = None) -> dict[str, Any]:
        if not settings.telegram_bot_token:
            raise RuntimeError("Telegram bot token is not configured")

        url = f"{settings.telegram_api_base_url}/bot{settings.telegram_bot_token}/answerCallbackQuery"
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return await asyncio.to_thread(self._post_json, url, payload)

    async def send_message(
        self,
        *,
        destination: str,
        body: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> ProviderSendResult:
        if not settings.telegram_bot_token:
            raise RuntimeError("Telegram bot token is not configured")

        url = f"{settings.telegram_api_base_url}/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": destination,
            "text": body,
            "disable_web_page_preview": False,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response_data = await asyncio.to_thread(self._post_json, url, payload)
        message_id = response_data.get("result", {}).get("message_id")
        return ProviderSendResult(
            provider_message_id=str(message_id) if message_id is not None else None,
            raw_response=response_data,
        )

    async def send_photo(
        self,
        *,
        destination: str,
        photo_url: str,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> ProviderSendResult:
        if not settings.telegram_bot_token:
            raise RuntimeError("Telegram bot token is not configured")

        url = f"{settings.telegram_api_base_url}/bot{settings.telegram_bot_token}/sendPhoto"
        payload: dict[str, Any] = {
            "chat_id": destination,
            "photo": photo_url,
        }
        if caption:
            payload["caption"] = caption
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response_data = await asyncio.to_thread(self._post_json, url, payload)
        message_id = response_data.get("result", {}).get("message_id")
        return ProviderSendResult(
            provider_message_id=str(message_id) if message_id is not None else None,
            raw_response=response_data,
        )

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=settings.telegram_send_timeout_seconds) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Telegram API failed with status {exc.code}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError("Telegram API is unavailable") from exc

        if response_data.get("ok") is not True:
            raise RuntimeError(str(response_data.get("description") or "Telegram API did not accept message"))
        return response_data


class SmsMessageProvider(MessageProvider):
    channel = MessageChannel.sms

    def __init__(self, sms_service: SmsService | None = None) -> None:
        self.sms_service = sms_service or SmsService()

    async def send_message(
        self,
        *,
        destination: str,
        body: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> ProviderSendResult:
        await self.sms_service.send_message(destination, body)
        return ProviderSendResult(provider_message_id=None, raw_response={"provider": settings.sms_provider})


class MessagingService:
    def __init__(self, providers: dict[MessageChannel, MessageProvider] | None = None) -> None:
        self.providers = providers or {
            MessageChannel.telegram: TelegramMessageProvider(),
            MessageChannel.sms: SmsMessageProvider(),
        }

    def campaign_message_body(self, campaign: Campaign) -> str | None:
        metadata = campaign.metadata_json or {}
        metadata_body = metadata.get("message_body")
        if isinstance(metadata_body, str) and metadata_body.strip():
            return metadata_body
        if campaign.template is not None:
            return campaign.template.body
        return None

    def validate_template_body(self, body: str) -> None:
        unknown = sorted(
            (
                set(VARIABLE_PATTERN.findall(body))
                | set(HASH_VARIABLE_PATTERN.findall(body))
                | set(BRACE_VARIABLE_PATTERN.findall(body))
            )
            - ALLOWED_TEMPLATE_VARIABLES
        )
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown template variables: {', '.join(unknown)}",
            )

    def render_template(self, body: str, variables: dict[str, str]) -> str:
        self.validate_template_body(body)

        def replace(match: re.Match[str]) -> str:
            return variables.get(match.group(1), "")

        return BRACE_VARIABLE_PATTERN.sub(replace, HASH_VARIABLE_PATTERN.sub(replace, VARIABLE_PATTERN.sub(replace, body)))

    def communication_allowed(
        self,
        preference: ClientCommunicationPreference | None,
        purpose: MessagePurpose,
    ) -> tuple[bool, str | None]:
        if preference and preference.do_not_contact:
            return False, "Client is marked do-not-contact"
        if preference and preference.blacklisted_at is not None:
            return False, "Client is blacklisted"
        if purpose in MARKETING_PURPOSES:
            if preference is None:
                return False, "Client has no marketing consent"
            if preference.marketing_consent != ConsentStatus.opted_in:
                return False, "Client opted out of marketing messages"
        else:
            if preference and preference.transactional_consent == ConsentStatus.opted_out:
                return False, "Client opted out of transactional messages"
        return True, None

    def build_idempotency_key(self, campaign_id: int, customer_id: int, appointment_id: int | None = None) -> str:
        target = appointment_id if appointment_id is not None else "none"
        return f"campaign:{campaign_id}:customer:{customer_id}:appointment:{target}"

    def recipient_destination(
        self,
        recipient: MessageRecipient,
        preference: ClientCommunicationPreference | None,
    ) -> tuple[str | None, str | None]:
        if recipient.channel == MessageChannel.telegram:
            if preference is None or not preference.telegram_chat_id:
                return None, "Client has no Telegram chat_id"
            return preference.telegram_chat_id, None
        if recipient.channel == MessageChannel.sms:
            phone = getattr(recipient.customer, "phone", None)
            if not phone:
                return None, "Client has no phone"
            return phone, None
        return None, f"No destination resolver for channel {recipient.channel.value}"

    async def get_template(self, session: AsyncSession, template_id: int) -> MessageTemplate:
        template = await session.get(MessageTemplate, template_id)
        if template is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message template not found")
        return template

    async def get_campaign(self, session: AsyncSession, campaign_id: int) -> Campaign:
        stmt = (
            select(Campaign)
            .options(selectinload(Campaign.audience_filter), selectinload(Campaign.template))
            .where(Campaign.id == campaign_id)
        )
        campaign = (await session.execute(stmt)).scalar_one_or_none()
        if campaign is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        return campaign

    async def create_template(self, session: AsyncSession, data: dict[str, Any]) -> MessageTemplate:
        self.validate_template_body(data["body"])
        template = MessageTemplate(**data)
        session.add(template)
        await session.commit()
        await session.refresh(template)
        return template

    async def update_template(self, session: AsyncSession, template: MessageTemplate, data: dict[str, Any]) -> MessageTemplate:
        if "body" in data and data["body"] is not None:
            self.validate_template_body(data["body"])
        for key, value in data.items():
            setattr(template, key, value)
        await session.commit()
        await session.refresh(template)
        return template

    async def create_campaign(self, session: AsyncSession, data: dict[str, Any], audience: AudienceCriteria | None) -> Campaign:
        if data.get("template_id") is not None:
            await self.get_template(session, data["template_id"])
        metadata = data.get("metadata_json")
        if isinstance(metadata, dict) and isinstance(metadata.get("message_body"), str):
            self.validate_template_body(metadata["message_body"])
        campaign = Campaign(**data)
        if audience is not None:
            campaign.audience_filter = CampaignAudienceFilter(criteria=audience.model_dump(mode="json", exclude_none=True))
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)
        return await self.get_campaign(session, campaign.id)

    async def update_campaign(
        self,
        session: AsyncSession,
        campaign: Campaign,
        data: dict[str, Any],
        audience: AudienceCriteria | None,
    ) -> Campaign:
        if data.get("template_id") is not None:
            await self.get_template(session, data["template_id"])
        metadata = data.get("metadata_json")
        if isinstance(metadata, dict) and isinstance(metadata.get("message_body"), str):
            self.validate_template_body(metadata["message_body"])
        for key, value in data.items():
            setattr(campaign, key, value)
        if audience is not None:
            if campaign.audience_filter is None:
                campaign.audience_filter = CampaignAudienceFilter(criteria=audience.model_dump(mode="json", exclude_none=True))
            else:
                campaign.audience_filter.criteria = audience.model_dump(mode="json", exclude_none=True)
        await session.commit()
        return await self.get_campaign(session, campaign.id)

    def audience_from_campaign(self, campaign: Campaign) -> AudienceCriteria:
        if campaign.audience_filter is None:
            return AudienceCriteria(all_clients=True)
        return AudienceCriteria.model_validate(campaign.audience_filter.criteria)

    async def calculate_recipients(self, session: AsyncSession, campaign: Campaign) -> Sequence[Customer]:
        criteria = self.audience_from_campaign(campaign)
        stmt = select(Customer).distinct().where(Customer.is_active.is_(True))
        if not criteria.all_clients:
            stmt = stmt.join(Booking, Booking.customer_id == Customer.id, isouter=True)

            filters = []
            if criteria.barber_ids:
                filters.append(Booking.master_id.in_(criteria.barber_ids))
            if criteria.visited_from is not None:
                filters.append(Booking.start_at >= criteria.visited_from)
            if criteria.visited_to is not None:
                filters.append(Booking.start_at <= criteria.visited_to)
            if criteria.service_ids:
                service_booking_ids = select(BookingServiceItem.booking_id).where(
                    BookingServiceItem.service_id.in_(criteria.service_ids)
                )
                filters.append(or_(Booking.service_id.in_(criteria.service_ids), Booking.id.in_(service_booking_ids)))
            if filters:
                stmt = stmt.where(and_(*filters))

            if criteria.inactive_days is not None:
                cutoff = datetime.now().astimezone() - timedelta(days=criteria.inactive_days)
                latest_visit = (
                    select(func.max(Booking.start_at))
                    .where(Booking.customer_id == Customer.id, Booking.status == BookingStatus.completed)
                    .correlate(Customer)
                    .scalar_subquery()
                )
                stmt = stmt.where(or_(latest_visit.is_(None), latest_visit < cutoff))
            if criteria.first_time_clients:
                booking_count = (
                    select(func.count(Booking.id))
                    .where(Booking.customer_id == Customer.id, Booking.status == BookingStatus.completed)
                    .correlate(Customer)
                    .scalar_subquery()
                )
                stmt = stmt.where(booking_count <= 1)
            if criteria.vip_clients:
                min_spent = criteria.vip_min_total_spent if criteria.vip_min_total_spent is not None else 10000
                stmt = stmt.where(Customer.imported_total_spent >= min_spent)
            if criteria.birthday_month is not None:
                stmt = stmt.where(func.extract("month", Customer.birthday) == criteria.birthday_month)

        if criteria.limit is not None:
            stmt = stmt.limit(criteria.limit)
        return (await session.execute(stmt.order_by(Customer.id.asc()))).scalars().all()

    async def build_variables(
        self,
        session: AsyncSession,
        customer: Customer,
        campaign: Campaign | None = None,
        appointment: Booking | None = None,
        extra_variables: dict[str, str] | None = None,
    ) -> dict[str, str]:
        client_name = " ".join(part for part in [customer.name, customer.surname] if part).strip() or customer.phone
        appointment_start = appointment.start_at.astimezone(KYIV_TZ) if appointment is not None else None
        appointment_services = list(getattr(appointment, "services", []) or []) if appointment is not None else []
        if not appointment_services and appointment is not None and appointment.service is not None:
            appointment_services = [appointment.service]
        service_name = ", ".join(
            (getattr(service_item, "title_uk", None) or service_item.name)
            for service_item in appointment_services
        )
        appointment_date = appointment_start.strftime("%d.%m.%Y") if appointment_start is not None else ""
        appointment_time = appointment_start.strftime("%H:%M") if appointment_start is not None else ""
        appointment_end_value = getattr(appointment, "end_at", None) if appointment is not None else None
        appointment_end = appointment_end_value.astimezone(KYIV_TZ) if appointment_end_value is not None else None
        appointment_end_time = appointment_end.strftime("%H:%M") if appointment_end is not None else ""
        appointment_datetime = " ".join(part for part in (appointment_date, appointment_time) if part)
        variables = {
            "client": client_name,
            "client_name": client_name,
            "customer_name": client_name,
            "barber_name": appointment.master.full_name if appointment is not None and appointment.master is not None else "",
            "master_name": appointment.master.full_name if appointment is not None and appointment.master is not None else "",
            "date": appointment_datetime,
            "appointment_date": appointment_date,
            "appointment_time": appointment_time,
            "appointment_end_time": appointment_end_time,
            "service": service_name,
            "service_name": service_name,
            "barbershop_name": settings.barbershop_name,
            "review_link": (campaign.review_url if campaign and campaign.review_url else settings.messaging_default_review_url) or "",
            "discount_code": campaign.discount_code if campaign and campaign.discount_code else "",
        }
        variables.update(extra_variables or {})
        return variables

    async def render_for_customer(
        self,
        session: AsyncSession,
        body: str,
        customer: Customer,
        campaign: Campaign | None = None,
        appointment: Booking | None = None,
        extra_variables: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        variables = await self.build_variables(session, customer, campaign, appointment, extra_variables)
        return self.render_template(body, variables), variables

    async def enqueue_campaign_recipients(
        self,
        session: AsyncSession,
        campaign: Campaign,
        scheduled_at: datetime | None = None,
    ) -> int:
        if not self.campaign_message_body(campaign):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Campaign has no message body")
        customers = await self.calculate_recipients(session, campaign)
        count = 0
        for customer in customers:
            count += await self.enqueue_recipient(session, campaign, customer, None, scheduled_at or campaign.scheduled_at)
        await session.commit()
        return count

    async def enqueue_recipient(
        self,
        session: AsyncSession,
        campaign: Campaign,
        customer: Customer,
        appointment: Booking | None,
        scheduled_at: datetime | None = None,
    ) -> int:
        idempotency_key = self.build_idempotency_key(campaign.id, customer.id, appointment.id if appointment else None)
        existing_id = (
            await session.execute(select(MessageRecipient.id).where(MessageRecipient.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing_id is not None:
            return 0

        preference = await self.get_preference(session, customer.id)
        allowed, reason = self.communication_allowed(preference, campaign.purpose)
        recipient = MessageRecipient(
            campaign_id=campaign.id,
            customer_id=customer.id,
            appointment_id=appointment.id if appointment else None,
            channel=campaign.channel,
            status=MessageDeliveryStatus.pending if allowed else MessageDeliveryStatus.skipped,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
            last_error=reason,
        )
        body = self.campaign_message_body(campaign)
        if body is not None:
            rendered, _ = await self.render_for_customer(session, body, customer, campaign, appointment)
            recipient.rendered_message = rendered
        session.add(recipient)
        await session.flush()
        if recipient.status == MessageDeliveryStatus.skipped:
            session.add(
                MessageLog(
                    campaign_id=campaign.id,
                    recipient_id=recipient.id,
                    customer_id=customer.id,
                    appointment_id=appointment.id if appointment else None,
                    channel=campaign.channel,
                    status=MessageDeliveryStatus.skipped,
                    error_reason=reason,
                )
            )
        return 1

    async def get_preference(self, session: AsyncSession, customer_id: int) -> ClientCommunicationPreference | None:
        return (
            await session.execute(
                select(ClientCommunicationPreference).where(ClientCommunicationPreference.customer_id == customer_id)
            )
        ).scalar_one_or_none()

    async def upsert_preference(
        self,
        session: AsyncSession,
        customer_id: int,
        data: dict[str, Any],
    ) -> ClientCommunicationPreference:
        customer = await session.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        preference = await self.get_preference(session, customer_id)
        if preference is None:
            preference = ClientCommunicationPreference(customer_id=customer_id)
            session.add(preference)
        if data.get("marketing_consent") == ConsentStatus.opted_out or data.get("do_not_contact") is True:
            preference.opted_out_at = datetime.now().astimezone()
        for key, value in data.items():
            setattr(preference, key, value)
        await session.commit()
        await session.refresh(preference)
        return preference

    async def process_pending_messages(self, session: AsyncSession, limit: int | None = None) -> int:
        now = datetime.now().astimezone()
        stmt = (
            select(MessageRecipient)
            .options(
                selectinload(MessageRecipient.customer),
                selectinload(MessageRecipient.campaign).selectinload(Campaign.template),
            )
            .where(
                MessageRecipient.status == MessageDeliveryStatus.pending,
                or_(MessageRecipient.scheduled_at.is_(None), MessageRecipient.scheduled_at <= now),
                or_(MessageRecipient.next_retry_at.is_(None), MessageRecipient.next_retry_at <= now),
            )
            .order_by(MessageRecipient.created_at.asc())
            .limit(limit or settings.messaging_batch_size)
        )
        recipients = (await session.execute(stmt)).scalars().all()
        processed = 0
        for recipient in recipients:
            await self.send_recipient(session, recipient)
            processed += 1
        await session.commit()
        return processed

    async def send_recipient(self, session: AsyncSession, recipient: MessageRecipient) -> None:
        campaign = recipient.campaign
        preference = await self.get_preference(session, recipient.customer_id)
        allowed, reason = self.communication_allowed(preference, campaign.purpose)
        if not allowed:
            recipient.status = MessageDeliveryStatus.skipped
            recipient.last_error = reason
            session.add(self._log_from_recipient(recipient, MessageDeliveryStatus.skipped, error_reason=reason))
            return
        destination, destination_error = self.recipient_destination(recipient, preference)
        if destination_error is not None:
            recipient.status = MessageDeliveryStatus.skipped
            recipient.last_error = destination_error
            session.add(self._log_from_recipient(recipient, MessageDeliveryStatus.skipped, error_reason=recipient.last_error))
            return
        if recipient.rendered_message is None:
            body = self.campaign_message_body(campaign)
            if body is None:
                recipient.status = MessageDeliveryStatus.failed
                recipient.last_error = "Campaign has no message body"
                return
            appointment = await session.get(Booking, recipient.appointment_id) if recipient.appointment_id else None
            rendered, _ = await self.render_for_customer(session, body, recipient.customer, campaign, appointment)
            recipient.rendered_message = rendered

        provider = self.providers.get(recipient.channel)
        if provider is None:
            recipient.status = MessageDeliveryStatus.failed
            recipient.last_error = f"No provider configured for channel {recipient.channel.value}"
            session.add(self._log_from_recipient(recipient, MessageDeliveryStatus.failed, error_reason=recipient.last_error))
            return

        recipient.attempts += 1
        try:
            result = await provider.send_message(destination=destination, body=recipient.rendered_message)
        except Exception as exc:  # pragma: no cover - exact provider exceptions vary
            recipient.last_error = str(exc)
            if recipient.attempts >= settings.messaging_max_retry_attempts:
                recipient.status = MessageDeliveryStatus.failed
            else:
                recipient.next_retry_at = datetime.now().astimezone() + timedelta(minutes=settings.messaging_retry_delay_minutes)
            session.add(self._log_from_recipient(recipient, recipient.status, error_reason=recipient.last_error))
            logger.warning("Message send failed", extra={"recipient_id": recipient.id, "error": recipient.last_error})
            return

        recipient.status = MessageDeliveryStatus.sent
        recipient.sent_at = datetime.now().astimezone()
        recipient.provider_message_id = result.provider_message_id
        recipient.last_error = None
        session.add(self._log_from_recipient(recipient, MessageDeliveryStatus.sent, provider_response=result.raw_response))
        await self.mark_review_request_sent(session, recipient)

    def _log_from_recipient(
        self,
        recipient: MessageRecipient,
        status_value: MessageDeliveryStatus,
        provider_response: dict[str, Any] | None = None,
        error_reason: str | None = None,
    ) -> MessageLog:
        return MessageLog(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            customer_id=recipient.customer_id,
            appointment_id=recipient.appointment_id,
            channel=recipient.channel,
            status=status_value,
            provider_response=provider_response,
            error_reason=error_reason,
        )

    async def mark_review_request_sent(self, session: AsyncSession, recipient: MessageRecipient) -> None:
        review_request = (
            await session.execute(select(ReviewRequest).where(ReviewRequest.recipient_id == recipient.id))
        ).scalar_one_or_none()
        if review_request is not None and review_request.sent_at is None:
            review_request.sent_at = recipient.sent_at

    async def retry_failed(self, session: AsyncSession, campaign_id: int | None = None) -> int:
        stmt = select(MessageRecipient).where(MessageRecipient.status == MessageDeliveryStatus.failed)
        if campaign_id is not None:
            stmt = stmt.where(MessageRecipient.campaign_id == campaign_id)
        recipients = (await session.execute(stmt)).scalars().all()
        for recipient in recipients:
            recipient.status = MessageDeliveryStatus.pending
            recipient.next_retry_at = None
            recipient.last_error = None
        await session.commit()
        return len(recipients)

    async def create_review_requests_for_completed_appointments(self, session: AsyncSession) -> int:
        now = datetime.now().astimezone()
        campaigns = (
            await session.execute(
                select(Campaign)
                .options(selectinload(Campaign.template))
                .where(
                    Campaign.type == CampaignType.post_visit_review_request,
                    Campaign.status == CampaignStatus.active,
                    Campaign.template_id.is_not(None),
                    Campaign.review_url.is_not(None),
                )
            )
        ).scalars().all()
        created = 0
        for campaign in campaigns:
            delay = timedelta(minutes=campaign.review_delay_minutes or 60)
            stmt = (
                select(Booking)
                .options(selectinload(Booking.customer), selectinload(Booking.master), selectinload(Booking.service))
                .where(
                    Booking.status == BookingStatus.completed,
                    Booking.customer_id.is_not(None),
                    Booking.completed_at.is_not(None),
                    Booking.completed_at <= now - delay,
                )
            )
            bookings = (await session.execute(stmt)).scalars().all()
            for booking in bookings:
                if booking.customer is None:
                    continue
                added = await self.enqueue_recipient(session, campaign, booking.customer, booking, now)
                if not added:
                    continue
                recipient = (
                    await session.execute(
                        select(MessageRecipient).where(
                            MessageRecipient.idempotency_key
                            == self.build_idempotency_key(campaign.id, booking.customer.id, booking.id)
                        )
                    )
                ).scalar_one()
                session.add(
                    ReviewRequest(
                        campaign_id=campaign.id,
                        appointment_id=booking.id,
                        customer_id=booking.customer.id,
                        platform=campaign.review_platform or ReviewPlatform.custom,
                        review_url=campaign.review_url or "",
                        recipient_id=recipient.id,
                    )
                )
                created += 1
        await session.commit()
        return created

    async def create_appointment_reminders_for_upcoming_bookings(self, session: AsyncSession) -> int:
        now = datetime.now(KYIV_TZ)
        campaigns = (
            await session.execute(
                select(Campaign)
                .options(selectinload(Campaign.template))
                .where(
                    Campaign.type == CampaignType.appointment_reminder,
                    Campaign.status == CampaignStatus.active,
                    Campaign.template_id.is_not(None),
                )
            )
        ).scalars().all()
        created = 0
        booking_service_items = selectinload(Booking.service_items).selectinload(BookingServiceItem.service)
        for campaign in campaigns:
            metadata = campaign.metadata_json or {}
            lead_hours = int(metadata.get("lead_hours") or 24)
            window_minutes = int(metadata.get("window_minutes") or 60)
            window_start = now + timedelta(hours=lead_hours)
            window_end = window_start + timedelta(minutes=window_minutes)
            bookings = (
                await session.execute(
                    select(Booking)
                    .options(
                        selectinload(Booking.customer),
                        selectinload(Booking.master),
                        selectinload(Booking.service),
                        booking_service_items,
                    )
                    .where(
                        Booking.status == BookingStatus.confirmed,
                        Booking.customer_id.is_not(None),
                        Booking.start_at >= window_start,
                        Booking.start_at < window_end,
                    )
                    .order_by(Booking.start_at.asc())
                )
            ).scalars().all()
            for booking in bookings:
                if booking.customer is None:
                    continue
                created += await self.enqueue_recipient(session, campaign, booking.customer, booking, now)
        await session.commit()
        return created

    async def analytics(self, session: AsyncSession, campaign_id: int | None = None) -> dict[str, Any]:
        base_filter = [MessageRecipient.campaign_id == campaign_id] if campaign_id is not None else []
        counts = dict.fromkeys([item.value for item in MessageDeliveryStatus], 0)
        rows = (
            await session.execute(
                select(MessageRecipient.status, func.count(MessageRecipient.id)).where(*base_filter).group_by(MessageRecipient.status)
            )
        ).all()
        for status_value, count in rows:
            counts[status_value.value] = count
        total = sum(counts.values())
        performance_rows = (
            await session.execute(
                select(func.date(MessageLog.created_at), MessageLog.status, func.count(MessageLog.id))
                .where(*(base_filter if campaign_id is None else [MessageLog.campaign_id == campaign_id]))
                .group_by(func.date(MessageLog.created_at), MessageLog.status)
                .order_by(func.date(MessageLog.created_at).asc())
            )
        ).all()
        failed_messages = (
            await session.execute(
                select(MessageRecipient.id, MessageRecipient.customer_id, MessageRecipient.last_error)
                .where(MessageRecipient.status == MessageDeliveryStatus.failed, *base_filter)
                .order_by(MessageRecipient.updated_at.desc())
                .limit(50)
            )
        ).all()
        review_sent_count = (
            await session.execute(
                select(func.count(ReviewRequest.id)).where(
                    ReviewRequest.sent_at.is_not(None),
                    *([ReviewRequest.campaign_id == campaign_id] if campaign_id is not None else []),
                )
            )
        ).scalar_one()
        sent_count = counts[MessageDeliveryStatus.sent.value]
        return {
            "campaign_id": campaign_id,
            "total_recipients": total,
            "sent_count": sent_count,
            "failed_count": counts[MessageDeliveryStatus.failed.value],
            "skipped_count": counts[MessageDeliveryStatus.skipped.value],
            "pending_count": counts[MessageDeliveryStatus.pending.value],
            "delivery_rate": round(sent_count / total, 4) if total else 0.0,
            "review_request_sent_count": review_sent_count,
            "performance_by_date": [
                {"date": str(day), "status": status_value.value, "count": count}
                for day, status_value, count in performance_rows
            ],
            "failed_messages": [
                {"recipient_id": recipient_id, "customer_id": customer_id, "error_reason": error_reason}
                for recipient_id, customer_id, error_reason in failed_messages
            ],
        }
