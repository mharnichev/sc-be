"""Reusable segment audiences and immutable campaign execution snapshots.

Scheduled audiences are evaluated when a worker first claims a due run. Delivery
uses the existing providers. A durable send reservation deliberately fails closed
on an ambiguous provider response because providers do not accept idempotency keys.
"""
from __future__ import annotations

from copy import deepcopy
from math import ceil
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import String, and_, case, cast, func, literal, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings

from app.models.campaign_run import CampaignRun
from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignStatus, CampaignType, ClientCommunicationPreference,
    MessageChannel, MessageDeliveryStatus, MessagePurpose, MessageRecipient,
    MARKETING_CAMPAIGN_TYPES,
    TelegramDeliveryAttempt,
)
from app.models.segment import CustomerSegment, SegmentStatus

KYIV_TZ = ZoneInfo("Europe/Kyiv")
BATCH_SIZE = 500
OFFER_AUDIENCE_RULES = {"combine": "all", "conditions": [{"type": "last_visit_age", "min": 3, "max": 12,
    "unit": "calendar_months", "min_inclusive": True, "max_inclusive": True}],
    "exclusions": [{"type": "upcoming_booking", "present": True}]}
CONFIG_KEYS = (
    "segment_ids", "channel_strategy", "offer_audience_mode", "exclude_returned_since_snapshot",
    "exclude_upcoming_booking", "marketing_frequency_days",
    "sms_recipients_per_minute", "sending_window", "marketing_max_contacts", "marketing_cap_days",
)


def delivery_options(campaign: Campaign) -> dict[str, Any]:
    metadata = campaign.metadata_json or {}
    options = {
        "segment_ids": metadata.get("segment_ids", []),
        "channel_strategy": metadata.get("channel_strategy", "single"),
        "offer_audience_mode": metadata.get("offer_audience_mode", "last_visit_3_12"),
        "exclude_returned_since_snapshot": metadata.get("exclude_returned_since_snapshot", False),
        "exclude_upcoming_booking": metadata.get("exclude_upcoming_booking", False),
        "marketing_frequency_days": metadata.get("marketing_frequency_days", 7),
        "sms_recipients_per_minute": metadata.get("sms_recipients_per_minute", settings.sms_campaign_recipients_per_minute),
        "sending_window": metadata.get("sending_window"),
        "marketing_max_contacts": metadata.get("marketing_max_contacts"),
        "marketing_cap_days": metadata.get("marketing_cap_days"),
    }
    for key in ("sending_window", "sms_recipients_per_minute", "marketing_frequency_days",
                "marketing_max_contacts", "marketing_cap_days"):
        value = getattr(campaign, key, None)
        if value is not None:
            options[key] = value
    return options


OFFER_CONFIG_KEYS = ("offer_master_id", "offer_promotion_id", "offer_starts_at", "offer_expires_at",
                     "offer_service_ids", "master_name_for_message")


def offer_options(campaign):
    return jsonable_encoder({key: getattr(campaign, key, None) for key in OFFER_CONFIG_KEYS})


def offer_schedule(campaign, scheduled_at, now):
    """Offer launches cannot enqueue before validity or after expiry."""
    if getattr(campaign, "offer_master_id", None) is None:
        return scheduled_at
    starts = getattr(campaign, "offer_starts_at", None)
    expires = getattr(campaign, "offer_expires_at", None)
    if starts is None or expires is None:
        raise HTTPException(422, detail={"code": "offer_validity_required"})
    if scheduled_at is None:
        return starts if starts > now else None
    if not starts <= scheduled_at < expires:
        raise HTTPException(422, detail={"code": "launch_outside_offer_validity"})
    return scheduled_at


def sms_parts(body: str) -> int:
    """GSM-7 septet / UCS-2 multipart estimate, not provider billing."""
    gsm = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
    extension = set("\f^{}\\[~]|€")
    if all(char in gsm or char in extension for char in body):
        units = sum(2 if char in extension else 1 for char in body)
        return 1 if units <= 160 else ceil(units / 153)
    return 1 if len(body) <= 70 else ceil(len(body) / 67)


def choose_channel(customer: Customer, preference: ClientCommunicationPreference | None,
                   strategy: str, channel: MessageChannel | str) -> tuple[MessageChannel | None, str | None]:
    """Select one reachable channel. No fallback follows acceptance or unread state."""
    ordered = {
        "single": [MessageChannel(channel)],
        "telegram_then_sms": [MessageChannel.telegram, MessageChannel.sms],
        "sms_then_telegram": [MessageChannel.sms, MessageChannel.telegram],
    }[strategy]
    for candidate in ordered:
        if candidate == MessageChannel.telegram and preference and preference.telegram_chat_id:
            return candidate, None
        if candidate == MessageChannel.sms and customer.phone:
            return candidate, None
    return None, "channel_unreachable"


def marketing_contact_predicate(customer_id: Any, now: datetime, days: int, *, exclude_recipient_id: int | None = None,
                                max_contacts: int | None = None, cap_days: int | None = None):
    """Provider acceptance or a durable uncertain/in-flight claim counts as contact.

    Review requests and transactional notifications are outside this marketing cap.
    """
    cutoff = now - timedelta(days=days)
    stmt = (
        select(MessageRecipient.id).join(Campaign, Campaign.id == MessageRecipient.campaign_id)
        .outerjoin(CampaignRun, CampaignRun.id == MessageRecipient.run_id)
        .where(
            MessageRecipient.customer_id == customer_id,
            or_(
                and_(MessageRecipient.run_id.is_(None), Campaign.purpose == MessagePurpose.marketing,
                     Campaign.type.in_(MARKETING_CAMPAIGN_TYPES)),
                CampaignRun.campaign_snapshot["purpose"].as_string() == MessagePurpose.marketing.value,
            ),
            or_(MessageRecipient.sent_at >= cutoff, MessageRecipient.send_started_at >= cutoff),
        )
    )
    if exclude_recipient_id is not None:
        stmt = stmt.where(MessageRecipient.id != exclude_recipient_id)
    frequency = stmt.exists()
    if max_contacts is not None and cap_days is not None:
        count = select(func.count(MessageRecipient.id)).join(Campaign, Campaign.id == MessageRecipient.campaign_id).outerjoin(
            CampaignRun, CampaignRun.id == MessageRecipient.run_id).where(
            MessageRecipient.customer_id == customer_id,
            or_(and_(MessageRecipient.run_id.is_(None), Campaign.purpose == MessagePurpose.marketing, Campaign.type.in_(MARKETING_CAMPAIGN_TYPES)),
                CampaignRun.campaign_snapshot["purpose"].as_string() == MessagePurpose.marketing.value),
            or_(MessageRecipient.sent_at >= now - timedelta(days=cap_days), MessageRecipient.send_started_at >= now - timedelta(days=cap_days)))
        if exclude_recipient_id is not None:
            count = count.where(MessageRecipient.id != exclude_recipient_id)
        return or_(frequency, count.scalar_subquery() >= max_contacts)
    return frequency


class CampaignRunService:
    def __init__(self, messaging=None) -> None:
        if messaging is None:
            from app.services.messaging import MessagingService
            messaging = MessagingService()
        self.messaging = messaging

    async def prepare_campaign_data(self, session: AsyncSession, data: dict[str, Any],
                                    audience=None, campaign: Campaign | None = None) -> dict[str, Any]:
        """Map the typed API options into reserved, validated campaign metadata."""
        result = dict(data)
        previous = delivery_options(campaign) if campaign is not None else {
            "segment_ids": [], "channel_strategy": "single", "offer_audience_mode": "last_visit_3_12", "exclude_returned_since_snapshot": False,
            "exclude_upcoming_booking": False, "marketing_frequency_days": 7,
            "sms_recipients_per_minute": settings.sms_campaign_recipients_per_minute, "sending_window": None,
            "marketing_max_contacts": None, "marketing_cap_days": None,
        }
        metadata = deepcopy(result.get("metadata_json", campaign.metadata_json if campaign else {}) or {})
        mode = result.get("offer_audience_mode", metadata.get("offer_audience_mode", previous["offer_audience_mode"]))
        if mode not in {"last_visit_3_12", "segments"}:
            raise HTTPException(422, detail="Invalid offer_audience_mode")
        offer_master_id = result.get("offer_master_id", getattr(campaign, "offer_master_id", None))
        if offer_master_id is not None:
            effective_type = result.get("type", getattr(campaign, "type", None))
            if effective_type not in {CampaignType.manual, CampaignType.re_engagement}:
                raise HTTPException(422, detail="Offer campaigns require manual or re_engagement type")
            if str(metadata.get("recipient", "customer")).lower() != "customer":
                raise HTTPException(422, detail="Offer campaigns target customers only")
            if result.get("channel_strategy", metadata.get("channel_strategy", previous["channel_strategy"])) not in {"single", "telegram_then_sms"}:
                raise HTTPException(422, detail="Offer campaigns require SMS or Telegram then SMS channel strategy")
            if campaign is None or campaign.status == CampaignStatus.draft and result.get("status") == CampaignStatus.active:
                result["status"] = CampaignStatus.draft
            timezone = result.get("timezone", getattr(campaign, "timezone", "Europe/Kyiv"))
            if timezone not in {"Europe/Kyiv", "Europe/Kiev"}:
                raise HTTPException(422, detail="Offer campaigns require Europe/Kyiv timezone")
            result["timezone"] = "Europe/Kyiv"
            if not result.get("offer_starts_at", getattr(campaign, "offer_starts_at", None)) or not result.get("offer_expires_at", getattr(campaign, "offer_expires_at", None)):
                result["status"] = CampaignStatus.draft
            if campaign is None and result.get("sending_window") is None:
                result["sending_window"] = {"start": "10:00", "end": "18:00", "days": list(range(7))}
            effective_window = result.get("sending_window", previous["sending_window"])
            if effective_window is None:
                raise HTTPException(422, detail="Offer campaigns require a daytime sending_window")
            window = effective_window.model_dump() if hasattr(effective_window, "model_dump") else effective_window
            if not isinstance(window, dict) or not ("10:00" <= window.get("start", "") < window.get("end", "") <= "18:00"):
                raise HTTPException(422, detail="Offer campaigns send only between 10:00 and 18:00 Europe/Kyiv")
            channel = result.get("channel", getattr(campaign, "channel", MessageChannel.sms))
            purpose = result.get("purpose", getattr(campaign, "purpose", MessagePurpose.marketing))
            allowed_channels = {MessageChannel.sms, MessageChannel.telegram} if mode == "segments" else {MessageChannel.sms}
            if channel not in allowed_channels or purpose != MessagePurpose.marketing:
                raise HTTPException(422, detail="Invalid offer campaign channel or purpose")
            strategy = result.get("channel_strategy", metadata.get("channel_strategy", previous["channel_strategy"]))
            if mode == "segments" and strategy == "telegram_then_sms" and channel != MessageChannel.telegram:
                raise HTTPException(422, detail="Segment offers require Telegram channel for telegram_then_sms")

        for key in CONFIG_KEYS:
            if key in result:
                value = result.pop(key)
                if value is None and key not in {"sending_window", "marketing_max_contacts", "marketing_cap_days"}:
                    raise HTTPException(422, detail=f"{key} cannot be null")
                metadata[key] = getattr(value, "value", value)
            elif key not in metadata or (campaign is not None and getattr(campaign, key, None) is not None):
                metadata[key] = previous[key]
        ids = metadata["segment_ids"]
        if not isinstance(ids, list) or len(ids) > 20 or any(type(item) is not int or item <= 0 for item in ids):
            raise HTTPException(422, detail="segment_ids must contain at most 20 positive integer IDs")
        metadata["segment_ids"] = list(dict.fromkeys(ids))
        if offer_master_id is not None and mode == "segments" and not ids:
            raise HTTPException(422, detail="Segment offers require saved segment_ids")
        if metadata["channel_strategy"] not in {"single", "telegram_then_sms", "sms_then_telegram"}:
            raise HTTPException(422, detail="Invalid channel_strategy")
        if type(metadata["marketing_frequency_days"]) is not int or not 1 <= metadata["marketing_frequency_days"] <= 365:
            raise HTTPException(422, detail="marketing_frequency_days must be between 1 and 365")
        if type(metadata["sms_recipients_per_minute"]) is not int or not 1 <= metadata["sms_recipients_per_minute"] <= 480:
            raise HTTPException(422, detail="sms_recipients_per_minute must be between 1 and 480")
        if metadata["sending_window"] is not None:
            from pydantic import ValidationError
            from app.schemas.messaging import CampaignSendingWindow
            try:
                metadata["sending_window"] = CampaignSendingWindow.model_validate(metadata["sending_window"]).model_dump()
            except ValidationError as exc:
                raise HTTPException(422, detail="Invalid sending_window") from exc
        if any(type(metadata[key]) is not bool for key in ("exclude_returned_since_snapshot", "exclude_upcoming_booking")):
            raise HTTPException(422, detail="Campaign exclusion options must be boolean")
        if ids:
            if str(metadata.get("recipient", "customer")).lower() in {"master", "barber"}:
                raise HTTPException(422, detail="Customer segments cannot target masters")
            if audience is not None:
                raise HTTPException(422, detail="Use segment_ids or inline audience, not both")
            effective_type = result.get("type", campaign.type if campaign else None)
            effective_purpose = result.get("purpose", campaign.purpose if campaign else MessagePurpose.marketing)
            if effective_type not in {CampaignType.manual, CampaignType.re_engagement, "manual", "re_engagement"} or effective_purpose != MessagePurpose.marketing:
                raise HTTPException(422, detail="Reusable segments require a manual or re_engagement marketing campaign")
            await self._load_segments(session, ids)
            # A create never activates a reusable audience; launch is a separate operation.
            if campaign is None:
                result["status"] = CampaignStatus.draft
        for key in ("sending_window", "sms_recipients_per_minute", "marketing_frequency_days",
                    "marketing_max_contacts", "marketing_cap_days"):
            result[key] = metadata[key]
        for key in ("marketing_max_contacts", "marketing_cap_days"):
            value = metadata[key]
            if value is not None and (type(value) is not int or not 1 <= value <= 365):
                raise HTTPException(422, detail=f"{key} must be between 1 and 365")
        if (metadata["marketing_max_contacts"] is None) != (metadata["marketing_cap_days"] is None):
            raise HTTPException(422, detail="marketing_max_contacts and marketing_cap_days must be configured together")
        result["metadata_json"] = metadata
        return result

    async def _load_segments(self, session: AsyncSession, ids: list[int], *, lock: bool = False) -> list[CustomerSegment]:
        if not ids:
            return []
        stmt = select(CustomerSegment).where(CustomerSegment.id.in_(ids)).order_by(CustomerSegment.id)
        if lock:
            stmt = stmt.with_for_update(read=True)
        segments = list((await session.execute(stmt.execution_options(populate_existing=True))).scalars())
        if {segment.id for segment in segments} != set(ids):
            raise HTTPException(422, detail="One or more customer segments do not exist")
        if any(segment.status != SegmentStatus.active for segment in segments):
            raise HTTPException(409, detail="Archived segments cannot be used for a new snapshot")
        return segments

    def _audience_statement(self, campaign: Campaign, snapshots: list[dict[str, Any]], now: datetime):
        from app.services.segments import segment_service
        if snapshots:
            stmt = select(Customer).where(
                or_(*(segment_service.build_predicate(item["rules"], now) for item in snapshots)),
            )
        else:
            # Preserve the legacy audience's global limit before intersecting offer restrictions.
            legacy = self.messaging.legacy_audience_statement(campaign, evaluated_at=now)
            stmt = select(Customer).where(Customer.id.in_(legacy.with_only_columns(Customer.id)))
        if getattr(campaign, "offer_master_id", None) is not None and delivery_options(campaign)["offer_audience_mode"] == "last_visit_3_12":
            stmt = stmt.where(segment_service.build_predicate(OFFER_AUDIENCE_RULES, now))
        return stmt

    @staticmethod
    async def _snapshot_transaction(session: AsyncSession) -> None:
        # The API hands over a read-only request transaction. Never commit unrelated
        # caller writes merely to change isolation; a caller must persist them first.
        if session.new or session.deleted or any(session.is_modified(item) for item in session.dirty):
            raise HTTPException(409, detail="Persist pending changes before launching a campaign run")
        if session.in_transaction():
            await session.commit()
        if session.get_bind().dialect.name == "postgresql":
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    @staticmethod
    def _serialization_failure(exc: DBAPIError) -> bool:
        return getattr(exc.orig, "sqlstate", getattr(exc.orig, "pgcode", None)) in {"40001", "40P01"}

    async def readiness(self, session: AsyncSession, campaign: Campaign) -> dict[str, Any]:
        """Configuration readiness; runtime rollout still requires independent verification."""
        from app.models.booking import Master, BarberService
        from app.models.promotion import Promotion
        from app.services.sms import SmsService
        now = datetime.now(KYIV_TZ)
        checks = []
        from app.services.campaign_runtime import worker_healthy

        def check(code, ready, detail):
            checks.append({"code": code, "status": "ready" if ready else "blocked", "detail": detail})

        check("campaign_scheduler", settings.campaign_run_scheduler_enabled and worker_healthy("campaign-run-scheduler", max(60, settings.campaign_run_scheduler_interval_seconds * 3)), "Enabled scheduler must have a recent successful local iteration")
        options = delivery_options(campaign)
        segment_offer = getattr(campaign, "offer_master_id", None) is not None and options["offer_audience_mode"] == "segments"
        requires_sms = not segment_offer or campaign.channel == MessageChannel.sms or options["channel_strategy"] == "telegram_then_sms"
        if requires_sms:
            check("sms_worker", settings.sms_queue_worker_enabled and worker_healthy("sms-queue-worker", max(60, settings.sms_queue_poll_seconds * 3)), "Enabled SMS worker must have a recent successful local iteration")
            check("sms_provider", settings.sms_provider == "smsclub" and bool(settings.sms_club_token), "SMSClub provider and token are required")
        body = self.messaging.campaign_message_body(campaign)
        try:
            if requires_sms:
                SmsService.validate_message_body(body or "")
            valid_body = bool(body)
        except HTTPException:
            valid_body = False
        check("sms_template" if requires_sms else "template", valid_body,
              "A nonempty BMP-compatible SMS message is required" if requires_sms else "A nonempty Telegram message is required")
        master_id = getattr(campaign, "offer_master_id", None)
        promotion_id = getattr(campaign, "offer_promotion_id", None)
        if master_id is not None or promotion_id is not None:
            master = await session.get(Master, master_id) if master_id else None
            promotion = await session.get(Promotion, promotion_id) if promotion_id else None
            check("master", bool(master and master.is_active), "Select an active master")
            valid_discount = bool(promotion and (1 <= promotion.discount_percent <= 100 if segment_offer else promotion.discount_percent == 30))
            check("promotion", bool(promotion and promotion.is_active and valid_discount and promotion.discount_type.value == "percent" and promotion.recipient_offer_only), "Select an active recipient-only percentage promotion" if segment_offer else "Select the active 30 percent promotion")
            check("master_name", bool((getattr(campaign, "master_name_for_message", None) or "").strip()), "Configure the master's grammatical name for the message")
            window = delivery_options(campaign)["sending_window"]
            check("sending_window", bool(window and "10:00" <= window.get("start", "") < window.get("end", "") <= "18:00" and window.get("timezone", "Europe/Kyiv") == "Europe/Kyiv"), "Offers require a daytime Europe/Kyiv sending window")
            check("campaign_type", campaign.type in {CampaignType.manual, CampaignType.re_engagement}, "Offer campaigns require manual or re_engagement type")
            check("campaign_recipient", str((campaign.metadata_json or {}).get("recipient", "customer")).lower() == "customer", "Offer campaigns target customers only")
            strategy = delivery_options(campaign)["channel_strategy"]
            check("channel_strategy", strategy in {"single", "telegram_then_sms"}, "Offer campaigns require SMS or Telegram then SMS channel strategy")
            if strategy == "telegram_then_sms" or (segment_offer and campaign.channel == MessageChannel.telegram):
                check("telegram_provider", bool(settings.telegram_bot_token), "Telegram delivery requires a configured Telegram bot")
            valid_channel = campaign.channel in {MessageChannel.sms, MessageChannel.telegram} if segment_offer else campaign.channel == MessageChannel.sms
            if segment_offer and strategy == "telegram_then_sms":
                valid_channel = campaign.channel == MessageChannel.telegram
            check("campaign_channel", valid_channel and campaign.purpose == MessagePurpose.marketing, "Offer campaign channel must match its delivery strategy")
            if segment_offer:
                check("saved_segments", bool(options["segment_ids"]), "Segment offers require saved audience segments")
            starts = getattr(campaign, "offer_starts_at", None)
            expires = getattr(campaign, "offer_expires_at", None)
            check("offer_validity", bool(starts and expires and starts < expires and expires > now), "Offer start and future expiration are required")
            service_ids = getattr(campaign, "offer_service_ids", None) or []
            services = list((await session.scalars(select(BarberService).where(BarberService.id.in_(service_ids)))).all()) if service_ids else []
            check("services", bool(service_ids) and len(services) == len(set(service_ids)) and all(item.is_active and item.master_id == master_id for item in services), "Select active services for the chosen master")
        try:
            segments = await self._load_segments(session, delivery_options(campaign)["segment_ids"])
            stmt = self._audience_statement(campaign, [{"id": item.id, "rules": item.rules} for item in segments], now)
            count = await session.scalar(select(func.count()).select_from(stmt.subquery()))
            check("audience", bool(count), "Audience must contain at least one customer")
            if master_id and getattr(campaign, "offer_expires_at", None) and getattr(campaign, "master_name_for_message", None):
                preview = await self.preview(session, campaign)
                check("sms_eligible_audience", preview["communication_eligible_recipients"] > 0, "At least one communication-eligible recipient is required")
                unsupported = preview["personalization_invalid_recipients"] > 0
                check("personalized_sms", not unsupported, "Personalized SMS must use provider-supported characters")
        except HTTPException as exc:
            check("audience", False, str(exc.detail))
        return {"ready": all(item["status"] == "ready" for item in checks), "checks": checks,
                "runtime_verification": "required", "balance": {"status": "unsupported", "amount": None}}

    async def launch(self, session: AsyncSession, campaign: Campaign, scheduled_at: datetime | None = None,
                     idempotency_key: str | None = None, test_customer_id: int | None = None) -> CampaignRun:
        campaign_id = campaign.id
        for attempt in range(3):
            await self._snapshot_transaction(session)
            try:
                return await self._launch_once(session, campaign_id, scheduled_at, idempotency_key, test_customer_id)
            except DBAPIError as exc:
                await session.rollback()
                if not self._serialization_failure(exc) or attempt == 2:
                    raise
        raise AssertionError("Unreachable")

    async def _launch_once(self, session: AsyncSession, campaign_id: int, scheduled_at: datetime | None,
                           idempotency_key: str | None, test_customer_id: int | None = None) -> CampaignRun:
        # Fresh, locked configuration and one repeatable database view cover all
        # audience pages and their facts; concurrent same-key launches retry safely.
        campaign = (await session.execute(select(Campaign).where(Campaign.id == campaign_id)
            .options(selectinload(Campaign.template), selectinload(Campaign.audience_filter))
            .with_for_update().execution_options(populate_existing=True))).scalar_one()
        key = f"campaign:{campaign.id}:{idempotency_key or 'legacy-start'}"
        existing = (await session.execute(select(CampaignRun).where(CampaignRun.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            if (existing.campaign_snapshot or {}).get("test_customer_id") != test_customer_id:
                raise HTTPException(409, detail={"code": "launch_idempotency_conditions_changed"})
            return existing
        if getattr(campaign, "offer_master_id", None) is not None or getattr(campaign, "offer_promotion_id", None) is not None:
            readiness = await self.readiness(session, campaign)
            if not readiness["ready"]:
                raise HTTPException(409, detail={"code": "campaign_not_ready", "checks": readiness["checks"]})
        if campaign.status not in {CampaignStatus.draft, CampaignStatus.active, CampaignStatus.completed}:
            raise HTTPException(409, detail="Resume a paused campaign before launching; archived campaigns cannot launch")
        if str((campaign.metadata_json or {}).get("recipient", "customer")).lower() in {"master", "barber"}:
            raise HTTPException(422, detail="Customer campaign runs cannot target masters")
        allowed_types = (
            {CampaignType.manual, CampaignType.re_engagement}
            if delivery_options(campaign)["segment_ids"] else MARKETING_CAMPAIGN_TYPES
        )
        if campaign.type not in allowed_types:
            raise HTTPException(422, detail="Campaign runs require a customer marketing campaign type")
        if not self.messaging.campaign_message_body(campaign):
            raise HTTPException(422, detail="Campaign has no message body")
        now = datetime.now(KYIV_TZ)
        scheduled_at = scheduled_at or campaign.scheduled_at
        if scheduled_at is not None and scheduled_at.utcoffset() is None:
            raise HTTPException(422, detail="scheduled_at must include a timezone offset")
        scheduled_at = offer_schedule(campaign, scheduled_at, now)
        segments = await self._load_segments(session, delivery_options(campaign)["segment_ids"])
        if test_customer_id is not None:
            if getattr(campaign, "offer_master_id", None) is None:
                raise HTTPException(422, detail={"code": "offer_test_campaign_required"})
            statement = self._audience_statement(campaign, [{"id": item.id, "rules": item.rules} for item in segments], now)
            customer = (await session.scalars(statement.where(Customer.id == test_customer_id))).one_or_none()
            if customer is None:
                raise HTTPException(409, detail={"code": "test_recipient_ineligible"})
            preference = await self.messaging.get_preference(session, customer.id)
            allowed, reason = self.messaging.communication_allowed(preference, campaign.purpose)
            channel, channel_reason = choose_channel(customer, preference, delivery_options(campaign)["channel_strategy"], campaign.channel)
            options = delivery_options(campaign)
            capped = await session.scalar(select(marketing_contact_predicate(customer.id, now,
                options["marketing_frequency_days"], max_contacts=options.get("marketing_max_contacts"), cap_days=options.get("marketing_cap_days"))))
            if not allowed or reason or channel_reason or not channel or capped:
                raise HTTPException(409, detail={"code": "test_recipient_not_communication_eligible"})
        run = CampaignRun(campaign_id=campaign.id, idempotency_key=key, status="scheduled",
                          scheduled_at=scheduled_at, audience_count=0, campaign_snapshot=({"test_customer_id": test_customer_id} if test_customer_id is not None else {}), segment_snapshots=[])
        session.add(run)
        await session.flush()
        campaign.status = CampaignStatus.active
        if scheduled_at is None or scheduled_at <= now:
            await self.snapshot(session, run, campaign, now=now)
        await session.commit()
        await session.refresh(run)
        return run

    async def snapshot(self, session: AsyncSession, run: CampaignRun, campaign: Campaign,
                       *, now: datetime | None = None) -> None:
        from app.services.segments import segment_service
        if run.evaluated_at is not None:
            return
        now = now or datetime.now(KYIV_TZ)
        if getattr(campaign, "offer_master_id", None) is not None:
            starts = getattr(campaign, "offer_starts_at", None)
            expires = getattr(campaign, "offer_expires_at", None)
            if starts is None or expires is None or starts >= expires:
                raise HTTPException(422, detail={"code": "offer_validity_required"})
            if expires <= now:
                raise HTTPException(410, detail={"code": "offer_expired"})
        segments = await self._load_segments(session, delivery_options(campaign)["segment_ids"], lock=True)
        if getattr(campaign, "offer_master_id", None) is not None and delivery_options(campaign)["offer_audience_mode"] == "segments" and not segments:
            raise HTTPException(422, detail="Segment offers require saved segment_ids")
        run.segment_snapshots = [
            {"id": item.id, "name": item.name, "revision": item.revision, "rules": deepcopy(item.rules)}
            for item in segments
        ]
        body = self.messaging.campaign_message_body(campaign)
        if not body:
            raise HTTPException(422, detail="Campaign has no message body")
        test_customer_id = (run.campaign_snapshot or {}).get("test_customer_id")
        run.campaign_snapshot = {
            **({"test_customer_id": test_customer_id} if test_customer_id is not None else {}),
            **deepcopy(delivery_options(campaign)), **offer_options(campaign), "name": campaign.name, "type": campaign.type.value,
            "channel": campaign.channel.value, "purpose": campaign.purpose.value,
            "message_body": body, "template_id": campaign.template_id,
            "discount_code": campaign.discount_code, "review_url": campaign.review_url,
            "metadata_json": deepcopy(campaign.metadata_json or {}),
            "inline_audience": self.messaging.audience_from_campaign(campaign).model_dump(mode="json") if not segments else None,
        }
        if run.campaign_snapshot.get("offer_promotion_id") is not None:
            from app.models.promotion import Promotion
            promotion = await session.get(Promotion, run.campaign_snapshot["offer_promotion_id"])
            if run.campaign_snapshot["offer_audience_mode"] == "segments" and not (
                promotion and promotion.is_active and promotion.recipient_offer_only
                and promotion.discount_type.value == "percent" and 1 <= promotion.discount_percent <= 100
            ):
                raise HTTPException(409, detail={"code": "offer_promotion_unavailable"})
            run.campaign_snapshot = {**run.campaign_snapshot,
                                     "offer_discount_percent": promotion.discount_percent if run.campaign_snapshot["offer_audience_mode"] == "segments" else 30,
                                     "promotion_name_uk": promotion.name_uk,
                                     "promotion_name_en": promotion.name_en}
        run.evaluated_at = now
        stmt = self._audience_statement(campaign, run.segment_snapshots, now)
        if test_customer_id is not None:
            stmt = stmt.where(Customer.id == test_customer_id)
        after_id = 0
        while True:
            customers = list((await session.execute(stmt.where(Customer.id > after_id).order_by(Customer.id).limit(BATCH_SIZE))).scalars())
            if not customers:
                break
            ids = [customer.id for customer in customers]
            # Batch facts per segment, not per customer. These are immutable explanatory evidence.
            facts_by_segment = {
                item["id"]: await segment_service.member_facts(session, ids, item["rules"], now)
                for item in run.segment_snapshots
            }
            legacy_offer = run.campaign_snapshot.get("offer_master_id") is not None and run.campaign_snapshot["offer_audience_mode"] == "last_visit_3_12"
            offer_facts = await segment_service.member_facts(session, ids, OFFER_AUDIENCE_RULES, now) if legacy_offer else {}
            excluded_booking = set((await session.execute(select(Customer.id).where(
                Customer.id.in_(ids), segment_service.upcoming_booking_predicate(now)
            ))).scalars()) if run.campaign_snapshot.get("offer_master_id") is not None and run.campaign_snapshot["exclude_upcoming_booking"] else set()
            for customer in customers:
                facts = {str(segment_id): mapping.get(customer.id, {}) for segment_id, mapping in facts_by_segment.items()}
                recipient = MessageRecipient(
                    run_id=run.id, campaign_id=campaign.id, customer_id=customer.id,
                    channel=campaign.channel, status=MessageDeliveryStatus.pending,
                    idempotency_key=f"run:{run.id}:customer:{customer.id}", scheduled_at=now,
                    rendered_message=None, attempts=0,
                    snapshot_facts=jsonable_encoder({"segments": facts, "evaluated_at": now.isoformat()}),
                )
                session.add(recipient)
                extra_variables = None
                if run.campaign_snapshot.get("offer_master_id") is not None:
                    from app.services.campaign_offers import campaign_offer_service
                    await session.flush()
                    preference = await self.messaging.get_preference(session, customer.id)
                    allowed, consent_reason = self.messaging.communication_allowed(preference, campaign.purpose)
                    channel, channel_reason = choose_channel(customer, preference, run.campaign_snapshot["channel_strategy"], campaign.channel)
                    capped = await session.scalar(select(marketing_contact_predicate(customer.id, now,
                        run.campaign_snapshot["marketing_frequency_days"], max_contacts=run.campaign_snapshot.get("marketing_max_contacts"),
                        cap_days=run.campaign_snapshot.get("marketing_cap_days"))))
                    evidence = offer_facts.get(customer.id, {})
                    issued_snapshot = {**run.campaign_snapshot, "eligibility_evaluated_at": now.isoformat(),
                        "eligibility_facts": jsonable_encoder({key: evidence[key] for key in (
                            "history_state", "last_visit_at", "has_upcoming_booking", "conditions", "exclusions") if key in evidence}),
                        "communication_eligible": bool(
                        allowed and not consent_reason and channel and not channel_reason
                        and not capped and customer.id not in excluded_booking)}
                    _, token = await campaign_offer_service.issue_offer(session, recipient, issued_snapshot)
                    extra_variables = campaign_offer_service.render_variables(run.campaign_snapshot, token)
                recipient.rendered_message, _ = await self.messaging.render_for_customer(
                    session, body, customer, campaign, extra_variables=extra_variables)
                if recipient.channel == MessageChannel.sms or (
                    run.campaign_snapshot.get("offer_master_id") is not None
                    and run.campaign_snapshot["offer_audience_mode"] == "segments"
                    and run.campaign_snapshot["channel_strategy"] == "telegram_then_sms"
                ):
                    from app.services.sms import SmsService
                    SmsService.validate_message_body(recipient.rendered_message)

            run.audience_count += len(customers)
            after_id = customers[-1].id
            await session.flush()
        run.status = "snapshotted" if run.audience_count else "completed"
        await session.flush()

    async def process_due_runs(self, session: AsyncSession, *, limit: int = 20) -> int:
        for attempt in range(3):
            await self._snapshot_transaction(session)
            try:
                return await self._process_due_runs_once(session, limit=limit)
            except DBAPIError as exc:
                await session.rollback()
                if not self._serialization_failure(exc) or attempt == 2:
                    raise
        raise AssertionError("Unreachable")

    async def _process_due_runs_once(self, session: AsyncSession, *, limit: int) -> int:
        now = datetime.now(KYIV_TZ)
        runs = list((await session.execute(
            select(CampaignRun).join(Campaign, Campaign.id == CampaignRun.campaign_id)
            .where(CampaignRun.status == "scheduled", Campaign.status == CampaignStatus.active,
                   or_(CampaignRun.scheduled_at.is_(None), CampaignRun.scheduled_at <= now))
            .order_by(CampaignRun.id).limit(limit).with_for_update(skip_locked=True, of=CampaignRun)
        )).scalars())
        for run in runs:
            campaign = await self.messaging.get_campaign(session, run.campaign_id)
            try:
                async with session.begin_nested():
                    await self.snapshot(session, run, campaign, now=now)
            except HTTPException as exc:
                run.status = "failed"
                run.campaign_snapshot = {**(run.campaign_snapshot or {}), "snapshot_error": str(exc.detail)}
        await session.commit()
        return len(runs)

    async def process_run_messages(self, session: AsyncSession, limit: int | None = None) -> int:
        from app.services.messaging import _recipient_delivery_load_options
        now = datetime.now(KYIV_TZ)
        await self.reconcile_interrupted_sends(session, now=now, limit=limit or BATCH_SIZE)
        recipients = list((await session.execute(
            select(MessageRecipient).join(Campaign, Campaign.id == MessageRecipient.campaign_id)
            .options(*_recipient_delivery_load_options())
            .where(MessageRecipient.run_id.is_not(None), MessageRecipient.status == MessageDeliveryStatus.pending,
                   MessageRecipient.send_started_at.is_(None), Campaign.status == CampaignStatus.active,
                   MessageRecipient.sms_queue_job_id.is_(None),
                   or_(MessageRecipient.scheduled_at.is_(None), MessageRecipient.scheduled_at <= now),
                   or_(MessageRecipient.next_retry_at.is_(None), MessageRecipient.next_retry_at <= now))
            .order_by(MessageRecipient.id).limit(limit or BATCH_SIZE)
        )).scalars())
        for recipient in recipients:
            await self.send_recipient(session, recipient)
        return len(recipients)

    async def reconcile_interrupted_sends(self, session: AsyncSession, *, now: datetime | None = None,
                                          limit: int = BATCH_SIZE) -> int:
        """Surface abandoned durable claims; never turn them back into sendable work."""
        now = now or datetime.now(KYIV_TZ)
        accepted = list((await session.execute(select(MessageRecipient).join(TelegramDeliveryAttempt,
            TelegramDeliveryAttempt.key == literal("recipient:") + cast(MessageRecipient.id, String) + literal(":telegram"))
            .where(TelegramDeliveryAttempt.state == "accepted", MessageRecipient.channel == MessageChannel.telegram,
                MessageRecipient.status.in_([MessageDeliveryStatus.pending, MessageDeliveryStatus.failed]))
            .order_by(MessageRecipient.id).limit(limit).with_for_update(skip_locked=True, of=MessageRecipient)
            .execution_options(populate_existing=True))).scalars())
        run_ids = set()
        for recipient in accepted:
            attempt = await session.get(TelegramDeliveryAttempt, f"recipient:{recipient.id}:telegram")
            await self._project_accepted_telegram(session, recipient, attempt)
            if recipient.run_id is not None:
                run_ids.add(recipient.run_id)
        stale = list((await session.execute(select(MessageRecipient)
            .where(MessageRecipient.status == MessageDeliveryStatus.pending,
                   MessageRecipient.sms_queue_job_id.is_(None),
                   MessageRecipient.send_started_at <= now - timedelta(minutes=15))
            .order_by(MessageRecipient.id).limit(limit).with_for_update(skip_locked=True))).scalars())
        for recipient in stale:
            recipient.status = MessageDeliveryStatus.failed
            recipient.last_error = "delivery_uncertain: worker_interrupted"
            session.add(self.messaging._log_from_recipient(recipient, recipient.status, error_reason=recipient.last_error))
            if recipient.run_id is not None:
                run_ids.add(recipient.run_id)
        # A stable order avoids deadlocks when reconciling several runs in parallel.
        for run_id in sorted(run_ids):
            await self._complete_run_if_finished(session, await session.get(CampaignRun, run_id))
        await session.commit()
        return len(accepted) + len(stale)

    async def _project_accepted_telegram(self, session, recipient, attempt):
        recipient.status = MessageDeliveryStatus.sent
        recipient.sent_at = attempt.completed_at or attempt.started_at
        recipient.provider_message_id = attempt.provider_message_id
        recipient.last_error = None
        recipient.next_retry_at = None
        session.add(self.messaging._log_from_recipient(recipient, recipient.status,
            provider_response={"accepted": True, "provider_message_id": attempt.provider_message_id}))
        await self.messaging.mark_review_request_sent(session, recipient)

    async def preview(self, session: AsyncSession, campaign: Campaign, *, page: int = 1,
                      page_size: int = 50, _aggregate: bool = True) -> dict[str, Any]:
        if page < 1 or not 1 <= page_size <= BATCH_SIZE:
            raise HTTPException(422, detail="Invalid pagination")
        now = datetime.now(KYIV_TZ)
        options = delivery_options(campaign)
        segments = await self._load_segments(session, options["segment_ids"])
        snapshots = [{"id": item.id, "rules": item.rules} for item in segments]
        stmt = self._audience_statement(campaign, snapshots, now)
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        result = await self._preview_page(
            session, campaign, page=page, page_size=page_size, now=now,
            options=options, segments=segments, stmt=stmt, total=total, include_facts=True)
        if _aggregate:
            sms_eligible = 0
            telegram_eligible = 0
            total_parts = 0
            invalid_messages = 0
            for aggregate_page in range(1, ceil(total / BATCH_SIZE) + 1):
                chunk = result if page == aggregate_page and page_size == BATCH_SIZE else await self._preview_page(
                    session, campaign, page=aggregate_page, page_size=BATCH_SIZE, now=now,
                    options=options, segments=segments, stmt=stmt, total=total, include_facts=False)
                eligible = [item for item in chunk["items"] if item["eligible"] and item["channel"] == "sms"]
                invalid_messages += sum(not item["provider_supported"] for item in chunk["items"])
                sms_eligible += len(eligible)
                telegram_eligible += sum(item["eligible"] and item["channel"] == "telegram" for item in chunk["items"])
                total_parts += sum(item["sms_parts"] for item in eligible)
            result.update({"personalization_invalid_recipients": invalid_messages, "sms_eligible_recipients": sms_eligible, "telegram_eligible_recipients": telegram_eligible,
                           "communication_eligible_recipients": sms_eligible + telegram_eligible, "estimated_sms_parts": total_parts,
                           "estimated_cost": {"status": "unavailable", "amount": None},
                           "balance": {"status": "unsupported", "amount": None}})
        return result

    async def _preview_page(
        self, session: AsyncSession, campaign: Campaign, *, page: int, page_size: int,
        now: datetime, options: dict[str, Any], segments: list[CustomerSegment],
        stmt: Any, total: int, include_facts: bool,
    ) -> dict[str, Any]:
        from app.services.segments import segment_service
        customers = list((await session.execute(stmt.order_by(Customer.id).offset((page - 1) * page_size).limit(page_size))).scalars())
        ids = [item.id for item in customers]
        preferences = {item.customer_id: item for item in (await session.execute(
            select(ClientCommunicationPreference).where(ClientCommunicationPreference.customer_id.in_(ids))
        )).scalars()} if ids else {}
        facts = {item.id: await segment_service.member_facts(session, ids, item.rules, now) for item in segments} if ids and include_facts else {}
        excluded_booking = set((await session.execute(select(Customer.id).where(Customer.id.in_(ids),
            segment_service.upcoming_booking_predicate(now)))).scalars()) if ids and options["exclude_upcoming_booking"] else set()
        capped = set((await session.execute(select(Customer.id).where(Customer.id.in_(ids),
            marketing_contact_predicate(Customer.id, now, options["marketing_frequency_days"],
                max_contacts=options.get("marketing_max_contacts"), cap_days=options.get("marketing_cap_days"))))) .scalars()) if ids and campaign.purpose == MessagePurpose.marketing else set()
        items = []
        body = self.messaging.campaign_message_body(campaign) or ""
        preview_offer = offer_options(campaign)
        if getattr(campaign, "offer_promotion_id", None) is not None:
            from app.models.promotion import Promotion
            promotion = await session.get(Promotion, campaign.offer_promotion_id)
            if promotion:
                preview_offer.update(offer_discount_percent=promotion.discount_percent if options["offer_audience_mode"] == "segments" else 30,
                                     promotion_name_uk=promotion.name_uk, promotion_name_en=promotion.name_en)
        for customer in customers:
            preference = preferences.get(customer.id)
            allowed, reason = self.messaging.communication_allowed(preference, campaign.purpose)
            channel, channel_reason = choose_channel(customer, preference, options["channel_strategy"], campaign.channel)
            reason = reason or ("upcoming_booking" if customer.id in excluded_booking else None) or ("marketing_frequency_cap" if customer.id in capped else None) or channel_reason
            extra_variables = None
            if getattr(campaign, "offer_master_id", None) is not None:
                from app.services.campaign_offers import campaign_offer_service
                if getattr(campaign, "offer_expires_at", None) is not None and getattr(campaign, "master_name_for_message", None):
                    extra_variables = campaign_offer_service.render_variables(preview_offer, "A" * 43)
                else:
                    reason = reason or "offer_configuration_incomplete"
            if getattr(campaign, "offer_master_id", None) is not None and extra_variables is None:
                items.append({"customer_id": customer.id, "name": customer.name, "eligible": False,
                              "exclusion_reason": reason, "channel": channel.value if channel else None,
                              "provider_supported": True, "rendered_message": None, "sms_parts": None,
                              "reachability": {"sms": bool(customer.phone), "telegram": bool(preference and preference.telegram_chat_id)},
                              "facts": {str(key): values.get(customer.id, {}) for key, values in facts.items()}})
                continue
            rendered, _ = await self.messaging.render_for_customer(session, body, customer, campaign, extra_variables=extra_variables)
            from app.services.sms import SmsService
            provider_supported = True
            try:
                if not (getattr(campaign, "offer_master_id", None) is not None and options["offer_audience_mode"] == "segments") or channel == MessageChannel.sms or options["channel_strategy"] == "telegram_then_sms":
                    SmsService.validate_message_body(rendered)
            except HTTPException:
                provider_supported = False
                reason = reason or "unsupported_sms_characters"
            items.append({"customer_id": customer.id, "name": customer.name, "eligible": allowed and reason is None,
                          "exclusion_reason": reason, "channel": channel.value if channel else None,
                          "provider_supported": provider_supported, "rendered_message": rendered, "sms_parts": sms_parts(rendered) if channel == MessageChannel.sms else None,
                          "reachability": {"sms": bool(customer.phone), "telegram": bool(preference and preference.telegram_chat_id)},
                          "facts": {str(key): values.get(customer.id, {}) for key, values in facts.items()}})
        return {"evaluated_at": now, "total": total, "page": page, "page_size": page_size, "items": items}

    async def send_recipient(self, session: AsyncSession, recipient: MessageRecipient) -> None:
        from app.services.segments import segment_service
        # Serialize all marketing attempts for this customer, across campaigns.
        customer = (await session.execute(select(Customer).where(Customer.id == recipient.customer_id)
                    # Non-key updates serialize reservations without blocking the
                    # FK KEY SHARE checks performed by concurrent delivery logs.
                    .with_for_update(key_share=True).execution_options(populate_existing=True))).scalar_one()
        current = (await session.execute(select(MessageRecipient).where(MessageRecipient.id == recipient.id)
                   .with_for_update().execution_options(populate_existing=True))).scalar_one()
        attempt = await session.get(TelegramDeliveryAttempt, f"recipient:{current.id}:telegram")
        if attempt is not None and attempt.state == "accepted" and current.status in {MessageDeliveryStatus.pending, MessageDeliveryStatus.failed}:
            await self._project_accepted_telegram(session, current, attempt)
            await self._complete_run_if_finished(session, await session.get(CampaignRun, current.run_id) if current.run_id else None)
            await session.commit()
            return
        if attempt is not None and attempt.state in {"attempting", "ambiguous"}:
            await session.commit()
            return
        if current.status != MessageDeliveryStatus.pending or current.send_started_at is not None or current.sms_queue_job_id is not None:
            await session.commit()
            return
        now = datetime.now(KYIV_TZ)
        if (current.scheduled_at is not None and current.scheduled_at > now) or (current.next_retry_at is not None and current.next_retry_at > now):
            await session.commit()
            return
        run = await session.get(CampaignRun, current.run_id) if current.run_id else None
        campaign = (await session.execute(select(Campaign).where(Campaign.id == current.campaign_id)
            .options(selectinload(Campaign.template)).execution_options(populate_existing=True))).scalar_one()
        if campaign.status != CampaignStatus.active:
            await session.commit()
            return
        if run is not None and run.status == "cancelled":
            current.status = MessageDeliveryStatus.skipped
            current.last_error = "campaign_run_cancelled"
            session.add(self.messaging._log_from_recipient(current, current.status, error_reason=current.last_error))
            await session.commit()
            return
        snapshot = run.campaign_snapshot if run is not None else {
            **delivery_options(campaign), "channel": campaign.channel.value, "purpose": campaign.purpose.value,
        }
        purpose = MessagePurpose(snapshot["purpose"])
        from app.services.campaign_dispatch import sending_interval
        available_at, _ = sending_interval(now, snapshot.get("sending_window"))
        if available_at > now:
            current.next_retry_at = available_at
            await session.commit()
            return
        preference = await self.messaging.get_preference(session, customer.id)
        allowed, reason = self.messaging.communication_allowed(preference, purpose)
        if not reason and snapshot.get("exclude_upcoming_booking"):
            booked = (await session.execute(select(Customer.id).where(Customer.id == customer.id,
                segment_service.upcoming_booking_predicate(now)))).scalar_one_or_none()
            if booked is not None:
                reason = "upcoming_booking"
        if not reason and run and snapshot.get("exclude_returned_since_snapshot"):
            returned = (await session.execute(select(Customer.id).where(Customer.id == customer.id,
                segment_service.last_visit_at_expression(now) > run.evaluated_at))).scalar_one_or_none()
            if returned is not None:
                reason = "returned_since_snapshot"
        if not reason and purpose == MessagePurpose.marketing:
            capped = (await session.execute(select(marketing_contact_predicate(customer.id, now,
                snapshot.get("marketing_frequency_days", 7), exclude_recipient_id=current.id,
                max_contacts=snapshot.get("marketing_max_contacts"), cap_days=snapshot.get("marketing_cap_days"))))).scalar_one()
            if capped:
                reason = "marketing_frequency_cap"
        if not reason and snapshot.get("offer_master_id") is not None:
            from app.services.campaign_offers import campaign_offer_service
            reason = await campaign_offer_service.send_eligibility(session, current, now=now)
        channel, channel_reason = choose_channel(customer, preference, snapshot.get("channel_strategy", "single"), snapshot["channel"])
        reason = reason or channel_reason
        if reason or not allowed:
            current.status = MessageDeliveryStatus.skipped
            current.last_error = reason
            session.add(self.messaging._log_from_recipient(current, current.status, error_reason=reason))
            await self._complete_run_if_finished(session, run)
            await session.commit()
            return
        current.channel = channel
        provider = self.messaging.providers.get(channel)
        if run is None and current.rendered_message is None:
            body = self.messaging.campaign_message_body(campaign)
            if body is not None:
                current.rendered_message, _ = await self.messaging.render_for_customer(session, body, customer, campaign)
        if provider is None or current.rendered_message is None:
            current.status = MessageDeliveryStatus.failed
            current.last_error = "provider_unavailable" if provider is None else "snapshot_message_missing"
            session.add(self.messaging._log_from_recipient(current, current.status, error_reason=current.last_error))
            await self._complete_run_if_finished(session, run)
            await session.commit()
            return
        destination = preference.telegram_chat_id if channel == MessageChannel.telegram else customer.phone
        body = current.rendered_message
        if channel == MessageChannel.sms and getattr(provider, "uses_durable_queue", False):
            await self.messaging.enqueue_sms_recipient(session, current, provider, destination, body, priority=100)
            return
        # Commit BEFORE external I/O: a crash/timeout must never silently resend.
        current.send_started_at = now
        current.attempts += 1
        await session.commit()
        try:
            if channel == MessageChannel.telegram:
                from app.services.telegram_delivery import durable_telegram_send
                result = await durable_telegram_send(session, provider, key=f"recipient:{current.id}:telegram",
                    destination=destination, body=body)
            else:
                result = await provider.send_message(destination=destination, body=body)
        except Exception as exc:
            from app.services.telegram_delivery import TelegramDestinationUnavailable, TelegramRejected
            sms_provider = self.messaging.providers.get(MessageChannel.sms)
            if (isinstance(exc, TelegramDestinationUnavailable) and channel == MessageChannel.telegram
                    and snapshot.get("channel_strategy") == "telegram_then_sms" and customer.phone
                    and sms_provider is not None and getattr(sms_provider, "uses_durable_queue", False)):
                # Only a definitive destination rejection can release the Telegram claim.
                # The durable SMS queue repeats consent, offer, daytime and quota checks.
                session.add(self.messaging._log_from_recipient(current, MessageDeliveryStatus.failed,
                    error_reason="telegram_destination_unavailable_sms_fallback"))
                current.channel = MessageChannel.sms
                current.send_started_at = None
                current.last_error = "telegram_destination_unavailable_sms_fallback"
                await self.messaging.enqueue_sms_recipient(session, current, sms_provider, customer.phone, body, priority=100)
                return
            current.status = MessageDeliveryStatus.failed
            definitively_rejected = channel == MessageChannel.telegram and isinstance(exc, TelegramRejected)
            if definitively_rejected:
                current.send_started_at = None
            current.last_error = f"{'delivery_rejected' if definitively_rejected else 'delivery_uncertain'}: {type(exc).__name__}"
            session.add(self.messaging._log_from_recipient(current, current.status, error_reason=current.last_error))
        else:
            if channel == MessageChannel.telegram:
                current = (await session.execute(select(MessageRecipient).where(MessageRecipient.id == current.id)
                    .with_for_update().execution_options(populate_existing=True))).scalar_one()
                if current.status in {MessageDeliveryStatus.sent, MessageDeliveryStatus.delivered}:
                    await session.commit()
                    return
            current.status = MessageDeliveryStatus.sent
            current.sent_at = datetime.now(KYIV_TZ)
            current.provider_message_id = result.provider_message_id
            current.last_error = None
            session.add(self.messaging._log_from_recipient(current, current.status, provider_response=result.raw_response))
        await self._complete_run_if_finished(session, run)
        await session.commit()

    @staticmethod
    async def _complete_run_if_finished(session: AsyncSession, run: CampaignRun | None) -> None:
        if run is None:
            return
        await session.flush()
        # Two recipients can finish together. Serialize the final pending check so
        # the second finisher sees the first one's committed terminal outcome.
        run_status = (await session.execute(select(CampaignRun.status).where(CampaignRun.id == run.id).with_for_update())).scalar_one()
        if run_status == "cancelled":
            return
        pending = (await session.execute(select(MessageRecipient.id).where(MessageRecipient.run_id == run.id,
            MessageRecipient.status == MessageDeliveryStatus.pending).limit(1))).scalar_one_or_none()
        if pending is None:
            run.status = "completed"

    async def cancel_run_unsent(self, session: AsyncSession, run: CampaignRun) -> int:
        # Match dispatch locking so cancellation and transport reservations have a
        # clear winner. Already-dispatching work retains its observable outcome.
        await session.execute(select(Campaign.id).where(Campaign.id == run.campaign_id).with_for_update(key_share=True))
        from app.models.sms_queue import SmsQueueJob
        await session.execute(update(SmsQueueJob).where(
            or_(SmsQueueJob.status == "queued", and_(SmsQueueJob.status == "dispatching", SmsQueueJob.transport_started_at.is_(None))),
            SmsQueueJob.id.in_(select(MessageRecipient.sms_queue_job_id).where(MessageRecipient.run_id == run.id)),
        ).values(status="cancelled", error_code="campaign_run_cancelled", payload={}, outcome_projected_at=None, projection_retry_at=None,
                 lease_token=None, lease_expires_at=None,
                 attempts=case((SmsQueueJob.status == "dispatching", func.greatest(SmsQueueJob.attempts - 1, 0)), else_=SmsQueueJob.attempts)))
        cancelled_jobs = select(SmsQueueJob.id).where(SmsQueueJob.status == "cancelled", SmsQueueJob.error_code == "campaign_run_cancelled")
        result = await session.execute(update(MessageRecipient).where(
            MessageRecipient.run_id == run.id, MessageRecipient.status == MessageDeliveryStatus.pending,
            or_(and_(MessageRecipient.sms_queue_job_id.is_(None), MessageRecipient.send_started_at.is_(None)),
                MessageRecipient.sms_queue_job_id.in_(cancelled_jobs)),
        ).values(status=MessageDeliveryStatus.skipped, last_error="campaign_run_cancelled", next_retry_at=None, send_started_at=None))
        await session.execute(select(CampaignRun.id).where(CampaignRun.id == run.id).with_for_update())
        run.status = "cancelled"
        await session.commit()
        return result.rowcount


async def sms_job_eligibility(job):
    from app.services.campaign_dispatch import campaign_dispatch_service
    return await campaign_dispatch_service.sms_job_eligibility(job)


async def sms_job_outcome(job) -> None:
    from app.services.campaign_dispatch import campaign_dispatch_service
    await campaign_dispatch_service.sms_job_outcome(job)


campaign_run_service = CampaignRunService()
