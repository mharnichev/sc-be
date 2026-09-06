"""Reusable segment audiences and immutable campaign execution snapshots.

Scheduled audiences are evaluated when a worker first claims a due run. Delivery
uses the existing providers. A durable send reservation deliberately fails closed
on an ambiguous provider response because providers do not accept idempotency keys.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, case, func, or_, select, update
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
)
from app.models.segment import CustomerSegment, SegmentStatus

KYIV_TZ = ZoneInfo("Europe/Kyiv")
BATCH_SIZE = 500
CONFIG_KEYS = (
    "segment_ids", "channel_strategy", "exclude_returned_since_snapshot",
    "exclude_upcoming_booking", "marketing_frequency_days",
    "sms_recipients_per_minute", "sending_window",
)


def delivery_options(campaign: Campaign) -> dict[str, Any]:
    metadata = campaign.metadata_json or {}
    return {
        "segment_ids": metadata.get("segment_ids", []),
        "channel_strategy": metadata.get("channel_strategy", "single"),
        "exclude_returned_since_snapshot": metadata.get("exclude_returned_since_snapshot", False),
        "exclude_upcoming_booking": metadata.get("exclude_upcoming_booking", False),
        "marketing_frequency_days": metadata.get("marketing_frequency_days", 7),
        "sms_recipients_per_minute": metadata.get("sms_recipients_per_minute", settings.sms_campaign_recipients_per_minute),
        "sending_window": metadata.get("sending_window"),
    }


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


def marketing_contact_predicate(customer_id: Any, now: datetime, days: int, *, exclude_recipient_id: int | None = None):
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
    return stmt.exists()


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
            "segment_ids": [], "channel_strategy": "single", "exclude_returned_since_snapshot": False,
            "exclude_upcoming_booking": False, "marketing_frequency_days": 7,
            "sms_recipients_per_minute": settings.sms_campaign_recipients_per_minute, "sending_window": None,
        }
        metadata = deepcopy(result.get("metadata_json", campaign.metadata_json if campaign else {}) or {})
        for key in CONFIG_KEYS:
            if key in result:
                value = result.pop(key)
                if value is None and key != "sending_window":
                    raise HTTPException(422, detail=f"{key} cannot be null")
                metadata[key] = getattr(value, "value", value)
            elif key not in metadata:
                metadata[key] = previous[key]
        ids = metadata["segment_ids"]
        if not isinstance(ids, list) or len(ids) > 20 or any(type(item) is not int or item <= 0 for item in ids):
            raise HTTPException(422, detail="segment_ids must contain at most 20 positive integer IDs")
        metadata["segment_ids"] = list(dict.fromkeys(ids))
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
            return select(Customer).where(
                or_(*(segment_service.build_predicate(item["rules"], now) for item in snapshots)),
            )
        # Wrap a legacy limited selection so keyset paging preserves the original global limit.
        legacy = self.messaging.legacy_audience_statement(campaign, evaluated_at=now)
        return select(Customer).where(Customer.id.in_(legacy.with_only_columns(Customer.id)))

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

    async def launch(self, session: AsyncSession, campaign: Campaign, scheduled_at: datetime | None = None,
                     idempotency_key: str | None = None) -> CampaignRun:
        campaign_id = campaign.id
        for attempt in range(3):
            await self._snapshot_transaction(session)
            try:
                return await self._launch_once(session, campaign_id, scheduled_at, idempotency_key)
            except DBAPIError as exc:
                await session.rollback()
                if not self._serialization_failure(exc) or attempt == 2:
                    raise
        raise AssertionError("Unreachable")

    async def _launch_once(self, session: AsyncSession, campaign_id: int, scheduled_at: datetime | None,
                           idempotency_key: str | None) -> CampaignRun:
        # Fresh, locked configuration and one repeatable database view cover all
        # audience pages and their facts; concurrent same-key launches retry safely.
        campaign = (await session.execute(select(Campaign).where(Campaign.id == campaign_id)
            .options(selectinload(Campaign.template), selectinload(Campaign.audience_filter))
            .with_for_update().execution_options(populate_existing=True))).scalar_one()
        key = f"campaign:{campaign.id}:{idempotency_key or 'legacy-start'}"
        existing = (await session.execute(select(CampaignRun).where(CampaignRun.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            return existing
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
        await self._load_segments(session, delivery_options(campaign)["segment_ids"])
        run = CampaignRun(campaign_id=campaign.id, idempotency_key=key, status="scheduled",
                          scheduled_at=scheduled_at, audience_count=0, campaign_snapshot={}, segment_snapshots=[])
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
        segments = await self._load_segments(session, delivery_options(campaign)["segment_ids"], lock=True)
        run.segment_snapshots = [
            {"id": item.id, "name": item.name, "revision": item.revision, "rules": deepcopy(item.rules)}
            for item in segments
        ]
        body = self.messaging.campaign_message_body(campaign)
        if not body:
            raise HTTPException(422, detail="Campaign has no message body")
        run.campaign_snapshot = {
            **deepcopy(delivery_options(campaign)), "name": campaign.name, "type": campaign.type.value,
            "channel": campaign.channel.value, "purpose": campaign.purpose.value,
            "message_body": body, "template_id": campaign.template_id,
            "discount_code": campaign.discount_code, "review_url": campaign.review_url,
            "metadata_json": deepcopy(campaign.metadata_json or {}),
            "inline_audience": self.messaging.audience_from_campaign(campaign).model_dump(mode="json") if not segments else None,
        }
        run.evaluated_at = now
        stmt = self._audience_statement(campaign, run.segment_snapshots, now)
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
            for customer in customers:
                rendered, _ = await self.messaging.render_for_customer(session, body, customer, campaign)
                facts = {str(segment_id): mapping.get(customer.id, {}) for segment_id, mapping in facts_by_segment.items()}
                session.add(MessageRecipient(
                    run_id=run.id, campaign_id=campaign.id, customer_id=customer.id,
                    channel=campaign.channel, status=MessageDeliveryStatus.pending,
                    idempotency_key=f"run:{run.id}:customer:{customer.id}", scheduled_at=now,
                    rendered_message=rendered, attempts=0,
                    snapshot_facts=jsonable_encoder({"segments": facts, "evaluated_at": now.isoformat()}),
                ))
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
                run.campaign_snapshot = {"snapshot_error": str(exc.detail)}
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
        stale = list((await session.execute(select(MessageRecipient)
            .where(MessageRecipient.status == MessageDeliveryStatus.pending,
                   MessageRecipient.sms_queue_job_id.is_(None),
                   MessageRecipient.send_started_at <= now - timedelta(minutes=15))
            .order_by(MessageRecipient.id).limit(limit).with_for_update(skip_locked=True))).scalars())
        run_ids = set()
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
        return len(stale)

    async def preview(self, session: AsyncSession, campaign: Campaign, *, page: int = 1,
                      page_size: int = 50) -> dict[str, Any]:
        from app.services.segments import segment_service
        if page < 1 or not 1 <= page_size <= BATCH_SIZE:
            raise HTTPException(422, detail="Invalid pagination")
        now = datetime.now(KYIV_TZ)
        options = delivery_options(campaign)
        segments = await self._load_segments(session, options["segment_ids"])
        snapshots = [{"id": item.id, "rules": item.rules} for item in segments]
        stmt = self._audience_statement(campaign, snapshots, now)
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        customers = list((await session.execute(stmt.order_by(Customer.id).offset((page - 1) * page_size).limit(page_size))).scalars())
        ids = [item.id for item in customers]
        preferences = {item.customer_id: item for item in (await session.execute(
            select(ClientCommunicationPreference).where(ClientCommunicationPreference.customer_id.in_(ids))
        )).scalars()} if ids else {}
        facts = {item.id: await segment_service.member_facts(session, ids, item.rules, now) for item in segments} if ids else {}
        excluded_booking = set((await session.execute(select(Customer.id).where(Customer.id.in_(ids),
            segment_service.upcoming_booking_predicate(now)))).scalars()) if ids and options["exclude_upcoming_booking"] else set()
        capped = set((await session.execute(select(Customer.id).where(Customer.id.in_(ids),
            marketing_contact_predicate(Customer.id, now, options["marketing_frequency_days"])))) .scalars()) if ids and campaign.purpose == MessagePurpose.marketing else set()
        items = []
        for customer in customers:
            preference = preferences.get(customer.id)
            allowed, reason = self.messaging.communication_allowed(preference, campaign.purpose)
            if not customer.is_active:
                reason = "customer_inactive"
            channel, channel_reason = choose_channel(customer, preference, options["channel_strategy"], campaign.channel)
            reason = reason or ("upcoming_booking" if customer.id in excluded_booking else None) or ("marketing_frequency_cap" if customer.id in capped else None) or channel_reason
            items.append({"customer_id": customer.id, "name": customer.name, "eligible": allowed and reason is None,
                          "exclusion_reason": reason, "channel": channel.value if channel else None,
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
        if not customer.is_active:
            reason = "customer_inactive"
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
                snapshot.get("marketing_frequency_days", 7), exclude_recipient_id=current.id)))).scalar_one()
            if capped:
                reason = "marketing_frequency_cap"
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
            result = await provider.send_message(destination=destination, body=body)
        except Exception as exc:
            current.status = MessageDeliveryStatus.failed
            current.last_error = f"delivery_uncertain: {type(exc).__name__}"
            session.add(self.messaging._log_from_recipient(current, current.status, error_reason=current.last_error))
        else:
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
        ).values(status="cancelled", error_code="campaign_run_cancelled", payload={}, outcome_projected_at=None,
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
