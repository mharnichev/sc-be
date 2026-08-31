from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib import error, request
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
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
    ReviewRequestEvent,
    ReviewRequestStatus,
    ReviewPlatform,
)
from app.schemas.messaging import AudienceCriteria
from app.services.sms import SmsDeliveryStatus, SmsService
from app.services.master_reviews import (
    DELIVERY_REPORT_FAILURE_REASONS,
    generate_review_token,
    master_review_service,
)

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo("Europe/Kyiv")
_REVIEW_SCHEDULER_LOCK_ID = 1_397_966_934
_SMS_DELIVERY_SCHEDULER_LOCK_ID = 1_397_966_935

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
    "manage_url",
    "cancel_url",
    "month_name",
    "month",
    "coverage_percent",
    "low_coverage_percent",
    "target_percent",
}
BOOKING_CONFIRMATION_REQUIRED_TEMPLATE_VARIABLES = {"manage_url", "cancel_url"}
VARIABLE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
HASH_VARIABLE_PATTERN = re.compile(r"(?<![\w/])#([a-zA-Z_][a-zA-Z0-9_]*)\b")
BRACE_VARIABLE_PATTERN = re.compile(r"(?<!{){\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}(?!})")
MARKETING_PURPOSES = {MessagePurpose.marketing, MessagePurpose.review_request}


def _integer_set(values: object) -> set[int]:
    if not isinstance(values, list):
        return set()
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _review_request_is_within_frequency_cap(
    request_item: ReviewRequest,
    *,
    now: datetime,
    unanswered_days: int,
    submitted_days: int,
) -> bool:
    if (
        request_item.status == ReviewRequestStatus.failed
        and request_item.failure_reason not in DELIVERY_REPORT_FAILURE_REASONS
    ):
        return False
    submitted = request_item.status == ReviewRequestStatus.submitted or request_item.review_id is not None
    cap_days = submitted_days if submitted else unanswered_days
    if cap_days <= 0:
        return False
    reference_at = (request_item.reviewed_at or request_item.created_at) if submitted else request_item.created_at
    return reference_at > now - timedelta(days=cap_days)


async def _try_acquire_scheduler_lock(session: AsyncSession, lock_id: int) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        return True
    locked = (
        await session.execute(select(func.pg_try_advisory_xact_lock(lock_id)))
    ).scalar_one()
    if not locked:
        await session.rollback()
    return bool(locked)


async def _try_acquire_review_scheduler_lock(session: AsyncSession) -> bool:
    return await _try_acquire_scheduler_lock(session, _REVIEW_SCHEDULER_LOCK_ID)


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

    async def get_delivery_statuses(
        self,
        provider_message_ids: Sequence[str],
    ) -> dict[str, SmsDeliveryStatus]:
        return {}


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
        photo_url: str | None = None,
        photo_path: Path | None = None,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> ProviderSendResult:
        if not settings.telegram_bot_token:
            raise RuntimeError("Telegram bot token is not configured")
        if (photo_url is None) == (photo_path is None):
            raise ValueError("Exactly one Telegram photo source must be provided")

        url = f"{settings.telegram_api_base_url}/bot{settings.telegram_bot_token}/sendPhoto"
        if photo_path is not None:
            fields = {"chat_id": destination}
            if caption:
                fields["caption"] = caption
            if reply_markup is not None:
                fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            response_data = await asyncio.to_thread(self._post_multipart_file, url, fields, photo_path)
        else:
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

    def _post_multipart_file(self, url: str, fields: dict[str, str], photo_path: Path) -> dict[str, Any]:
        boundary = f"SoulcutsTelegram{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n'.encode(),
                    b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        filename = photo_path.name.replace('"', "").replace("\r", "").replace("\n", "") or "photo.jpg"
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode(),
                b"Content-Type: image/jpeg\r\n\r\n",
                photo_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        req = request.Request(
            url=url,
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._send_request(req)

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send_request(req)

    def _send_request(self, req: request.Request) -> dict[str, Any]:
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
        result = await self.sms_service.send_message(destination, body, sensitive=True)
        return ProviderSendResult(
            provider_message_id=result.provider_message_id,
            raw_response={
                "provider": settings.sms_provider,
                "accepted": True,
                "provider_message_id": result.provider_message_id,
            },
        )

    async def get_delivery_statuses(
        self,
        provider_message_ids: Sequence[str],
    ) -> dict[str, SmsDeliveryStatus]:
        return await self.sms_service.get_message_statuses(provider_message_ids)


def _recipient_delivery_load_options() -> tuple[object, ...]:
    return (
        selectinload(MessageRecipient.customer),
        selectinload(MessageRecipient.campaign).selectinload(Campaign.template),
        selectinload(MessageRecipient.appointment).selectinload(Booking.master),
        selectinload(MessageRecipient.appointment).selectinload(Booking.redirected_from_master),
        selectinload(MessageRecipient.appointment).selectinload(Booking.service),
        selectinload(MessageRecipient.appointment)
        .selectinload(Booking.service_items)
        .selectinload(BookingServiceItem.service),
    )


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

    @staticmethod
    def template_variables(body: str) -> set[str]:
        return (
            set(VARIABLE_PATTERN.findall(body))
            | set(HASH_VARIABLE_PATTERN.findall(body))
            | set(BRACE_VARIABLE_PATTERN.findall(body))
        )

    def validate_template_body(
        self,
        body: str,
        *,
        channel: MessageChannel | str | None = None,
    ) -> None:
        unknown = sorted(self.template_variables(body) - ALLOWED_TEMPLATE_VARIABLES)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown template variables: {', '.join(unknown)}",
            )
        if channel in {MessageChannel.sms, MessageChannel.sms.value}:
            SmsService.validate_message_body(body)

    def validate_booking_confirmation_template_body(self, body: str) -> None:
        self.validate_template_body(body)
        missing = sorted(
            BOOKING_CONFIRMATION_REQUIRED_TEMPLATE_VARIABLES
            - self.template_variables(body)
        )
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Booking confirmation SMS template must include: "
                    + ", ".join(f"{{{name}}}" for name in missing)
                ),
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
            if preference is not None and preference.marketing_consent == ConsentStatus.unknown:
                return False, "Client has no marketing consent"
            if preference is not None and preference.marketing_consent == ConsentStatus.opted_out:
                return False, "Client opted out of marketing messages"
        else:
            if preference and preference.transactional_consent == ConsentStatus.opted_out:
                return False, "Client opted out of transactional messages"
        return True, None

    @staticmethod
    def has_marketing_consent(preference: ClientCommunicationPreference | None) -> bool:
        return preference is None or preference.marketing_consent == ConsentStatus.opted_in

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
        self.validate_template_body(data["body"], channel=data.get("channel"))
        template = MessageTemplate(**data)
        session.add(template)
        await session.commit()
        await session.refresh(template)
        return template

    async def update_template(self, session: AsyncSession, template: MessageTemplate, data: dict[str, Any]) -> MessageTemplate:
        body = data.get("body", template.body)
        channel = data.get("channel", template.channel)
        if body is not None:
            self.validate_template_body(body, channel=channel)
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
            self.validate_template_body(
                metadata["message_body"],
                channel=data.get("channel"),
            )
        await self._validate_campaign_message_contract(session, data=data)
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
            self.validate_template_body(
                metadata["message_body"],
                channel=data.get("channel", campaign.channel),
            )
        await self._validate_campaign_message_contract(session, data=data, campaign=campaign)
        for key, value in data.items():
            setattr(campaign, key, value)
        if audience is not None:
            if campaign.audience_filter is None:
                campaign.audience_filter = CampaignAudienceFilter(criteria=audience.model_dump(mode="json", exclude_none=True))
            else:
                campaign.audience_filter.criteria = audience.model_dump(mode="json", exclude_none=True)
        await session.commit()
        return await self.get_campaign(session, campaign.id)

    async def _validate_campaign_message_contract(
        self,
        session: AsyncSession,
        *,
        data: dict[str, Any],
        campaign: Campaign | None = None,
    ) -> None:
        campaign_type = data.get("type", campaign.type if campaign is not None else None)
        channel = data.get("channel", campaign.channel if campaign is not None else None)
        location_key = data.get(
            "location_key",
            campaign.location_key if campaign is not None else None,
        )
        is_booking_confirmation = (
            campaign_type in {CampaignType.booking_confirmation, CampaignType.booking_confirmation.value}
            or location_key == "sms_booking_confirmation"
        )
        if channel not in {MessageChannel.sms, MessageChannel.sms.value} or not is_booking_confirmation:
            return

        metadata = data.get(
            "metadata_json",
            campaign.metadata_json if campaign is not None else {},
        )
        body = None
        if isinstance(metadata, dict):
            metadata_body = metadata.get("message_body")
            if isinstance(metadata_body, str) and metadata_body.strip():
                body = metadata_body
        if body is None:
            template_id = data.get(
                "template_id",
                campaign.template_id if campaign is not None else None,
            )
            if template_id is not None:
                body = (await self.get_template(session, template_id)).body
        if body is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Booking confirmation SMS campaign must have a message body",
            )
        self.validate_booking_confirmation_template_body(body)

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
        full_client_name = (
            " ".join(part for part in [customer.name, customer.surname] if part).strip()
            or customer.phone
        )
        client_name = (
            (customer.name or "").strip() or customer.phone
            if campaign is not None
            and getattr(campaign, "type", None) == CampaignType.post_visit_review_request
            else full_client_name
        )
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
        appointment_master = None
        if appointment is not None:
            appointment_master = getattr(appointment, "redirected_from_master", None) or getattr(
                appointment, "master", None
            )
        appointment_master_name = (
            getattr(appointment_master, "full_name", "") if appointment_master is not None else ""
        )
        variables = {
            "client": client_name,
            "client_name": client_name,
            "customer_name": client_name,
            "barber_name": appointment_master_name,
            "master_name": appointment_master_name,
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
        *,
        channel: MessageChannel | None = None,
        render_message: bool = True,
        extra_variables: dict[str, str] | None = None,
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
            channel=channel or campaign.channel,
            status=MessageDeliveryStatus.pending if allowed else MessageDeliveryStatus.skipped,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
            last_error=reason,
        )
        body = self.campaign_message_body(campaign)
        if body is not None and render_message:
            rendered, _ = await self.render_for_customer(
                session,
                body,
                customer,
                campaign,
                appointment,
                extra_variables=extra_variables,
            )
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
        if (
            data.get("marketing_consent") == ConsentStatus.opted_out
            or data.get("do_not_contact") is True
            or data.get("repeat_booking_opt_out") is True
        ):
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
            .options(*_recipient_delivery_load_options())
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
        review_request = (
            await session.execute(
                select(ReviewRequest)
                .options(selectinload(ReviewRequest.events))
                .where(ReviewRequest.recipient_id == recipient.id)
            )
        ).scalar_one_or_none()
        preference = await self.get_preference(session, recipient.customer_id)
        allowed, reason = self.communication_allowed(preference, campaign.purpose)
        if not allowed:
            recipient.status = MessageDeliveryStatus.skipped
            recipient.last_error = reason
            session.add(self._log_from_recipient(recipient, MessageDeliveryStatus.skipped, error_reason=reason))
            if review_request is not None:
                master_review_service.transition_request(
                    review_request,
                    ReviewRequestStatus.failed,
                    channel=recipient.channel,
                    reason="consent_not_granted",
                )
            return
        destination, destination_error = self.recipient_destination(recipient, preference)
        if (
            destination_error is not None
            and review_request is not None
            and recipient.channel == MessageChannel.telegram
            and review_request.fallback_channel == MessageChannel.sms
            and getattr(recipient.customer, "phone", None)
        ):
            recipient.channel = MessageChannel.sms
            review_request.channel = MessageChannel.sms
            master_review_service.transition_request(
                review_request,
                ReviewRequestStatus.scheduled,
                channel=MessageChannel.sms,
                reason="telegram_unavailable_fallback_to_sms",
            )
            destination, destination_error = self.recipient_destination(recipient, preference)
        if destination_error is not None:
            recipient.status = MessageDeliveryStatus.skipped
            recipient.last_error = destination_error
            session.add(self._log_from_recipient(recipient, MessageDeliveryStatus.skipped, error_reason=recipient.last_error))
            if review_request is not None:
                master_review_service.transition_request(
                    review_request,
                    ReviewRequestStatus.failed,
                    channel=recipient.channel,
                    reason="delivery_destination_unavailable",
                )
            return
        if review_request is not None:
            deferred_until = self.review_sms_deferred_until(campaign, recipient.channel)
            if deferred_until is not None:
                recipient.scheduled_at = deferred_until
                recipient.next_retry_at = None
                review_request.scheduled_at = deferred_until
                master_review_service.transition_request(
                    review_request,
                    ReviewRequestStatus.scheduled,
                    channel=recipient.channel,
                    reason="quiet_hours_deferred",
                )
                return
        message_body = recipient.rendered_message
        if review_request is not None:
            body = self.campaign_message_body(campaign)
            if body is None:
                recipient.status = MessageDeliveryStatus.failed
                recipient.last_error = "Campaign has no message body"
                master_review_service.transition_request(
                    review_request,
                    ReviewRequestStatus.failed,
                    channel=recipient.channel,
                    reason="template_unavailable",
                )
                return
            token, token_hash = generate_review_token()
            review_link = f"{settings.public_site_url.rstrip('/')}{settings.review_public_path.rstrip('/')}#{token}"
            appointment = recipient.appointment if recipient.appointment_id else None
            message_body, _ = await self.render_for_customer(
                session,
                body,
                recipient.customer,
                campaign,
                appointment,
                extra_variables={"review_link": review_link},
            )
            review_request.token_hash = token_hash
            review_request.expires_at = datetime.now(KYIV_TZ) + timedelta(days=settings.review_token_ttl_days)
        elif message_body is None:
            body = self.campaign_message_body(campaign)
            if body is None:
                recipient.status = MessageDeliveryStatus.failed
                recipient.last_error = "Campaign has no message body"
                return
            appointment = recipient.appointment if recipient.appointment_id else None
            rendered, _ = await self.render_for_customer(session, body, recipient.customer, campaign, appointment)
            recipient.rendered_message = rendered
            message_body = rendered

        provider = self.providers.get(recipient.channel)
        if provider is None:
            recipient.status = MessageDeliveryStatus.failed
            recipient.last_error = f"No provider configured for channel {recipient.channel.value}"
            session.add(self._log_from_recipient(recipient, MessageDeliveryStatus.failed, error_reason=recipient.last_error))
            if review_request is not None:
                master_review_service.transition_request(
                    review_request,
                    ReviewRequestStatus.failed,
                    channel=recipient.channel,
                    reason="provider_unavailable",
                )
            return

        recipient.attempts += 1
        try:
            result = await provider.send_message(destination=destination, body=message_body or "")
        except Exception as exc:  # pragma: no cover - exact provider exceptions vary
            recipient.last_error = str(exc)
            if recipient.attempts >= settings.messaging_max_retry_attempts:
                recipient.status = MessageDeliveryStatus.failed
            else:
                recipient.next_retry_at = datetime.now().astimezone() + timedelta(minutes=settings.messaging_retry_delay_minutes)
            session.add(self._log_from_recipient(recipient, recipient.status, error_reason=recipient.last_error))
            if review_request is not None and recipient.status == MessageDeliveryStatus.failed:
                if (
                    recipient.channel == MessageChannel.telegram
                    and review_request.fallback_channel == MessageChannel.sms
                    and getattr(recipient.customer, "phone", None)
                ):
                    recipient.channel = MessageChannel.sms
                    recipient.status = MessageDeliveryStatus.pending
                    recipient.attempts = 0
                    recipient.next_retry_at = datetime.now(KYIV_TZ)
                    recipient.last_error = None
                    review_request.channel = MessageChannel.sms
                    review_request.token_hash = None
                    review_request.expires_at = None
                    master_review_service.transition_request(
                        review_request,
                        ReviewRequestStatus.scheduled,
                        channel=MessageChannel.sms,
                        reason="telegram_failed_fallback_to_sms",
                    )
                else:
                    master_review_service.transition_request(
                        review_request,
                        ReviewRequestStatus.failed,
                        channel=recipient.channel,
                        reason="delivery_failed",
                    )
            logger.warning("Message send failed", extra={"recipient_id": recipient.id, "error": recipient.last_error})
            return

        recipient.status = MessageDeliveryStatus.sent
        recipient.sent_at = datetime.now().astimezone()
        recipient.provider_message_id = result.provider_message_id
        recipient.last_error = None
        safe_provider_response = (
            {"accepted": True, "provider_message_id": result.provider_message_id}
            if review_request is not None
            else result.raw_response
        )
        session.add(
            self._log_from_recipient(
                recipient,
                MessageDeliveryStatus.sent,
                provider_response=safe_provider_response,
            )
        )
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
            waitlist_request_id=recipient.waitlist_request_id,
            waitlist_offer_id=recipient.waitlist_offer_id,
            channel=recipient.channel,
            status=status_value,
            provider_response=provider_response,
            error_reason=error_reason,
        )

    async def mark_review_request_sent(self, session: AsyncSession, recipient: MessageRecipient) -> None:
        review_request = (
            await session.execute(
                select(ReviewRequest)
                .options(selectinload(ReviewRequest.events))
                .where(ReviewRequest.recipient_id == recipient.id)
            )
        ).scalar_one_or_none()
        if review_request is not None and review_request.status not in {
            ReviewRequestStatus.submitted,
            ReviewRequestStatus.expired,
        }:
            review_request.sent_at = recipient.sent_at
            review_request.delivered_at = None
            review_request.channel = recipient.channel
            master_review_service.transition_request(
                review_request,
                ReviewRequestStatus.sent,
                channel=recipient.channel,
            )

    async def sync_sms_delivery_statuses(self, session: AsyncSession, limit: int | None = None) -> int:
        if settings.sms_provider != "smsclub":
            return 0
        if not await _try_acquire_scheduler_lock(session, _SMS_DELIVERY_SCHEDULER_LOCK_ID):
            return 0

        provider = self.providers.get(MessageChannel.sms)
        if provider is None:
            return 0

        now = datetime.now(KYIV_TZ)
        stale_before = now - timedelta(seconds=settings.sms_delivery_status_poll_interval_seconds)
        sent_after = now - timedelta(hours=settings.sms_delivery_status_max_age_hours)
        batch_size = min(limit or settings.messaging_batch_size, 100)
        recipients = (
            await session.execute(
                select(MessageRecipient)
                .where(
                    MessageRecipient.channel == MessageChannel.sms,
                    MessageRecipient.status == MessageDeliveryStatus.sent,
                    MessageRecipient.provider_message_id.is_not(None),
                    MessageRecipient.sent_at.is_not(None),
                    MessageRecipient.sent_at >= sent_after,
                    or_(
                        MessageRecipient.delivery_status_checked_at.is_(None),
                        MessageRecipient.delivery_status_checked_at <= stale_before,
                    ),
                )
                .order_by(MessageRecipient.delivery_status_checked_at.asc().nullsfirst(), MessageRecipient.id.asc())
                .limit(batch_size)
            )
        ).scalars().all()
        if not recipients:
            return 0

        provider_message_ids = [
            recipient.provider_message_id
            for recipient in recipients
            if recipient.provider_message_id is not None
        ]
        statuses = await provider.get_delivery_statuses(provider_message_ids)
        terminal_recipients = [
            recipient
            for recipient in recipients
            if statuses.get(recipient.provider_message_id) not in {None, SmsDeliveryStatus.enroute}
        ]
        requests_by_recipient_id: dict[int, ReviewRequest] = {}
        if terminal_recipients:
            request_items = (
                await session.execute(
                    select(ReviewRequest)
                    .options(selectinload(ReviewRequest.events))
                    .where(ReviewRequest.recipient_id.in_([recipient.id for recipient in terminal_recipients]))
                )
            ).scalars().all()
            requests_by_recipient_id = {
                request_item.recipient_id: request_item
                for request_item in request_items
                if request_item.recipient_id is not None
            }

        updated = 0
        for recipient in recipients:
            recipient.delivery_status_checked_at = now
            provider_status = statuses.get(recipient.provider_message_id)
            if provider_status is None or provider_status == SmsDeliveryStatus.enroute:
                continue

            provider_response = {
                "provider": "smsclub",
                "provider_message_id": recipient.provider_message_id,
                "status": provider_status.value,
            }
            request_item = requests_by_recipient_id.get(recipient.id)
            if provider_status == SmsDeliveryStatus.delivered:
                recipient.status = MessageDeliveryStatus.delivered
                recipient.delivered_at = now
                recipient.last_error = None
                session.add(
                    self._log_from_recipient(
                        recipient,
                        MessageDeliveryStatus.delivered,
                        provider_response=provider_response,
                    )
                )
                if request_item is not None:
                    request_item.delivered_at = request_item.delivered_at or now
                    if request_item.status not in {
                        ReviewRequestStatus.delivered,
                        ReviewRequestStatus.submitted,
                        ReviewRequestStatus.expired,
                    }:
                        master_review_service.transition_request(
                            request_item,
                            ReviewRequestStatus.delivered,
                            channel=MessageChannel.sms,
                            reason="smsclub_delivery_confirmed",
                        )
            else:
                recipient.status = MessageDeliveryStatus.failed
                recipient.last_error = f"SMS Club delivery status: {provider_status.value}"
                session.add(
                    self._log_from_recipient(
                        recipient,
                        MessageDeliveryStatus.failed,
                        provider_response=provider_response,
                        error_reason=recipient.last_error,
                    )
                )
                if request_item is not None and request_item.status not in {
                    ReviewRequestStatus.submitted,
                    ReviewRequestStatus.expired,
                }:
                    master_review_service.transition_request(
                        request_item,
                        ReviewRequestStatus.failed,
                        channel=MessageChannel.sms,
                        reason=f"smsclub_{provider_status.value.lower()}",
                    )
            updated += 1

        await session.commit()
        return updated

    async def retry_failed(self, session: AsyncSession, campaign_id: int | None = None) -> int:
        stmt = select(MessageRecipient).where(MessageRecipient.status == MessageDeliveryStatus.failed)
        if campaign_id is not None:
            stmt = stmt.where(MessageRecipient.campaign_id == campaign_id)
        recipients = (await session.execute(stmt)).scalars().all()
        request_items = (
            (
                await session.execute(
                    select(ReviewRequest)
                    .options(selectinload(ReviewRequest.events))
                    .where(ReviewRequest.recipient_id.in_([recipient.id for recipient in recipients]))
                )
            )
            .scalars()
            .all()
            if recipients
            else []
        )
        requests_by_recipient_id = {
            request_item.recipient_id: request_item
            for request_item in request_items
            if request_item.recipient_id is not None
        }
        for recipient in recipients:
            recipient.status = MessageDeliveryStatus.pending
            recipient.next_retry_at = None
            recipient.last_error = None
            recipient.provider_message_id = None
            recipient.delivered_at = None
            recipient.delivery_status_checked_at = None
            request_item = requests_by_recipient_id.get(recipient.id)
            if request_item is not None and request_item.status not in {
                ReviewRequestStatus.submitted,
                ReviewRequestStatus.expired,
            }:
                request_item.token_hash = None
                request_item.expires_at = None
                request_item.delivered_at = None
                master_review_service.transition_request(
                    request_item,
                    ReviewRequestStatus.scheduled,
                    channel=recipient.channel,
                    reason="manual_retry_queued",
                )
        await session.commit()
        return len(recipients)

    async def create_review_requests_for_completed_appointments(self, session: AsyncSession) -> int:
        if not await _try_acquire_review_scheduler_lock(session):
            return 0
        now = datetime.now(KYIV_TZ)
        campaign = (
            await session.execute(
                select(Campaign)
                .options(selectinload(Campaign.template))
                .where(
                    Campaign.type == CampaignType.post_visit_review_request,
                    Campaign.status == CampaignStatus.active,
                    Campaign.template_id.is_not(None),
                )
                .order_by(Campaign.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if campaign is None:
            return 0
        created = 0
        metadata = campaign.metadata_json or {}
        send_time = str(metadata.get("send_time") or settings.review_daily_send_time)
        primary_channel = MessageChannel(metadata.get("primary_channel", MessageChannel.telegram.value))
        fallback_value = metadata.get("fallback_channel", MessageChannel.sms.value)
        fallback_channel = MessageChannel(fallback_value) if fallback_value else None
        quiet_from = str(metadata.get("quiet_hours_from") or settings.review_quiet_hours_from)
        quiet_to = str(metadata.get("quiet_hours_to") or settings.review_quiet_hours_to)
        quiet_hours_enabled = bool(metadata.get("quiet_hours_enabled", True))
        frequency_cap_days = max(0, int(metadata.get("frequency_cap_days", settings.review_frequency_cap_days)))
        submitted_frequency_cap_days = max(
            0,
            int(
                metadata.get(
                    "submitted_frequency_cap_days",
                    settings.review_submitted_frequency_cap_days,
                )
            ),
        )
        exclusions = dict(metadata.get("exclusions") or {})
        excluded_master_ids = _integer_set(exclusions.get("master_ids"))
        excluded_service_ids = _integer_set(exclusions.get("service_ids"))
        excluded_customer_ids = _integer_set(exclusions.get("customer_ids"))
        stmt = (
            select(Booking)
            .options(
                selectinload(Booking.customer),
                selectinload(Booking.master),
                selectinload(Booking.redirected_from_master),
                selectinload(Booking.service),
                selectinload(Booking.service_items),
            )
            .where(
                Booking.status == BookingStatus.completed,
                Booking.customer_id.is_not(None),
                Booking.completed_at.is_not(None),
                Booking.completed_at >= now - timedelta(hours=settings.review_request_lookback_hours),
                Booking.completed_at <= now,
                ~select(ReviewRequest.id).where(ReviewRequest.appointment_id == Booking.id).exists(),
            )
            .order_by(Booking.completed_at.asc())
        )
        bookings = (await session.execute(stmt)).scalars().all()
        for booking in bookings:
            if booking.customer is None:
                continue
            public_master_id = booking.public_master_id
            if (
                public_master_id in excluded_master_ids
                or booking.customer.id in excluded_customer_ids
                or excluded_service_ids.intersection(booking.service_ids)
            ):
                continue
            frequency_lookback_days = max(frequency_cap_days, submitted_frequency_cap_days)
            if frequency_lookback_days > 0:
                cutoff = now - timedelta(days=frequency_lookback_days)
                recent_requests = (
                    await session.execute(
                        select(ReviewRequest).where(
                            ReviewRequest.customer_id == booking.customer.id,
                            ReviewRequest.status != ReviewRequestStatus.failed,
                            or_(
                                ReviewRequest.created_at >= cutoff,
                                ReviewRequest.reviewed_at >= cutoff,
                            ),
                        )
                    )
                ).scalars().all()
                if any(
                    _review_request_is_within_frequency_cap(
                        request_item,
                        now=now,
                        unanswered_days=frequency_cap_days,
                        submitted_days=submitted_frequency_cap_days,
                    )
                    for request_item in recent_requests
                ):
                    continue
            completed_at = booking.completed_at
            if completed_at is None:
                continue
            visit_at = booking.end_at or booking.start_at or completed_at
            scheduled_at = self.next_day_review_send_at(visit_at, send_time=send_time)
            if scheduled_at < now:
                scheduled_at = self.next_day_review_send_at(now, send_time=send_time)
            if quiet_hours_enabled:
                scheduled_at = self.adjust_for_quiet_hours(
                    scheduled_at,
                    quiet_from=quiet_from,
                    quiet_to=quiet_to,
                )
            try:
                async with session.begin_nested():
                    added = await self.enqueue_recipient(
                        session,
                        campaign,
                        booking.customer,
                        booking,
                        scheduled_at,
                        channel=primary_channel,
                        render_message=False,
                    )
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
                    request_status = (
                        ReviewRequestStatus.scheduled
                        if recipient.status == MessageDeliveryStatus.pending
                        else ReviewRequestStatus.failed
                    )
                    request_item = ReviewRequest(
                        campaign_id=campaign.id,
                        appointment_id=booking.id,
                        customer_id=booking.customer.id,
                        master_id=public_master_id,
                        platform=ReviewPlatform.internal,
                        review_url=settings.review_public_path,
                        recipient_id=recipient.id,
                        scheduled_at=scheduled_at,
                        channel=primary_channel,
                        fallback_channel=fallback_channel,
                        status=request_status,
                        failure_reason=(
                            "consent_not_granted" if request_status == ReviewRequestStatus.failed else None
                        ),
                    )
                    request_item.events.append(
                        ReviewRequestEvent(
                            status=request_status,
                            channel=primary_channel,
                            reason=request_item.failure_reason,
                        )
                    )
                    session.add(request_item)
                    await session.flush()
                    created += 1
            except IntegrityError:
                # Another worker won either booking/request idempotency constraint.
                continue
        await session.commit()
        return created

    @staticmethod
    def next_day_review_send_at(value: datetime, *, send_time: str = "10:00") -> datetime:
        local = value.astimezone(KYIV_TZ) if value.tzinfo else value.replace(tzinfo=KYIV_TZ)
        hour, minute = (int(part) for part in send_time.split(":", maxsplit=1))
        return datetime.combine(
            local.date() + timedelta(days=1),
            time(hour=hour, minute=minute),
            tzinfo=KYIV_TZ,
        )

    @staticmethod
    def adjust_for_quiet_hours(value: datetime, *, quiet_from: str, quiet_to: str) -> datetime:
        local = value.astimezone(KYIV_TZ) if value.tzinfo else value.replace(tzinfo=KYIV_TZ)
        from_hour, from_minute = (int(part) for part in quiet_from.split(":", maxsplit=1))
        to_hour, to_minute = (int(part) for part in quiet_to.split(":", maxsplit=1))
        start_minutes = from_hour * 60 + from_minute
        end_minutes = to_hour * 60 + to_minute
        current_minutes = local.hour * 60 + local.minute
        in_quiet_hours = (
            start_minutes <= current_minutes < end_minutes
            if start_minutes < end_minutes
            else current_minutes >= start_minutes or current_minutes < end_minutes
        )
        if not in_quiet_hours:
            return local
        next_day = current_minutes >= start_minutes and start_minutes >= end_minutes
        target = local.replace(hour=to_hour, minute=to_minute, second=0, microsecond=0)
        return target + timedelta(days=1) if next_day else target

    @classmethod
    def review_sms_deferred_until(
        cls,
        campaign: Campaign,
        channel: MessageChannel,
        *,
        now: datetime | None = None,
    ) -> datetime | None:
        if channel != MessageChannel.sms:
            return None
        metadata = campaign.metadata_json or {}
        if not bool(metadata.get("quiet_hours_enabled", True)):
            return None
        value = now or datetime.now(KYIV_TZ)
        current = value.astimezone(KYIV_TZ) if value.tzinfo else value.replace(tzinfo=KYIV_TZ)
        adjusted = cls.adjust_for_quiet_hours(
            current,
            quiet_from=str(metadata.get("quiet_hours_from") or settings.review_quiet_hours_from),
            quiet_to=str(metadata.get("quiet_hours_to") or settings.review_quiet_hours_to),
        )
        return adjusted if adjusted > current else None

    async def process_pending_review_requests(self, session: AsyncSession) -> int:
        if not await _try_acquire_review_scheduler_lock(session):
            return 0
        now = datetime.now(KYIV_TZ)
        recipients = (
            await session.execute(
                select(MessageRecipient)
                .join(ReviewRequest, ReviewRequest.recipient_id == MessageRecipient.id)
                .options(*_recipient_delivery_load_options())
                .where(
                    MessageRecipient.status == MessageDeliveryStatus.pending,
                    or_(MessageRecipient.scheduled_at.is_(None), MessageRecipient.scheduled_at <= now),
                    or_(MessageRecipient.next_retry_at.is_(None), MessageRecipient.next_retry_at <= now),
                )
                .order_by(MessageRecipient.created_at.asc())
                .limit(settings.messaging_batch_size)
            )
        ).scalars().all()
        for recipient in recipients:
            await self.send_recipient(session, recipient)
        await session.commit()
        return len(recipients)

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
                        selectinload(Booking.redirected_from_master),
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
        delivered_count = counts[MessageDeliveryStatus.delivered.value]
        sent_count = counts[MessageDeliveryStatus.sent.value] + delivered_count
        return {
            "campaign_id": campaign_id,
            "total_recipients": total,
            "sent_count": sent_count,
            "delivered_count": delivered_count,
            "failed_count": counts[MessageDeliveryStatus.failed.value],
            "skipped_count": counts[MessageDeliveryStatus.skipped.value],
            "pending_count": counts[MessageDeliveryStatus.pending.value],
            "delivery_rate": round(delivered_count / total, 4) if total else 0.0,
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


async def run_review_request_scheduler() -> None:
    """Continuously discovers late completion updates and drains only review-request messages."""

    service = MessagingService()
    while True:
        await asyncio.sleep(settings.review_request_scheduler_interval_seconds)
        try:
            async with AsyncSessionLocal() as session:
                await service.create_review_requests_for_completed_appointments(session)
                await service.process_pending_review_requests(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Review request scheduler iteration failed")


async def run_sms_delivery_status_scheduler() -> None:
    """Continuously reconciles SMS Club delivery receipts for recently sent messages."""

    service = MessagingService()
    while True:
        await asyncio.sleep(settings.sms_delivery_status_poll_interval_seconds)
        try:
            async with AsyncSessionLocal() as session:
                await service.sync_sms_delivery_statuses(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("SMS delivery status scheduler iteration failed")
