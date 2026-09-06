"""Transport-time controls and observable progress for durable campaign SMS."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.campaign_run import CampaignRun
from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignStatus, MessageChannel, MessageDeliveryStatus, MessagePurpose,
    MessageRecipient, ReviewRequest, ReviewRequestStatus,
)
from app.models.sms_queue import SmsAccountThrottle, SmsQueueJob

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def _wall_time(day, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    # Explicitly normalize DST gaps; repeated times use the earlier occurrence.
    return datetime.combine(day, time(hour, minute), KYIV_TZ).replace(fold=0).astimezone(UTC)


def sending_interval(now: datetime, window: dict[str, Any] | None) -> tuple[datetime, datetime | None]:
    """Current/next allowed interval; overnight windows belong to their start day."""
    if now.utcoffset() is None:
        raise ValueError("Sending-window evaluation requires an aware timestamp")
    now = now.astimezone(UTC)
    if window is None:
        return now, None
    local_day = now.astimezone(KYIV_TZ).date()
    overnight = window["end"] < window["start"]
    for delta in range(-1, 8):
        day = local_day + timedelta(days=delta)
        if day.weekday() not in window["days"]:
            continue
        start = _wall_time(day, window["start"])
        end = _wall_time(day + timedelta(days=1 if overnight else 0), window["end"])
        if end > now:
            return max(start, now), end
    raise ValueError("Sending window has no permitted weekday")


def estimated_finish(now: datetime, seconds: float, window: dict[str, Any] | None) -> datetime | None:
    remaining = max(0, seconds)
    cursor = now.astimezone(UTC)
    for _ in range(3660):
        start, end = sending_interval(cursor, window)
        if end is None or (end - start).total_seconds() >= remaining:
            return start + timedelta(seconds=remaining)
        remaining -= (end - start).total_seconds()
        cursor = end
    return None


class CampaignDispatchService:
    def __init__(self, session_factory=None, messaging=None, clock=None) -> None:
        from app.services.messaging import MessagingService
        self.session_factory = session_factory or AsyncSessionLocal
        self.messaging = messaging or MessagingService()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def final_gate(self, session, job, now: datetime):
        """Last lifecycle/window check under the queue's campaign admission lock."""
        from app.services.campaign_runs import delivery_options
        from app.services.sms_queue import SmsDispatchDecision
        context = job.context_json or {}
        campaign_id = context.get("campaign_id")
        if campaign_id is None or job.operation != "send":
            return SmsDispatchDecision(action="allow")
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None or campaign.status == CampaignStatus.archived:
            return SmsDispatchDecision(action="skip", reason="campaign_archived")
        run = await session.get(CampaignRun, context["run_id"]) if context.get("run_id") else None
        if run is not None and run.status == "cancelled":
            return SmsDispatchDecision(action="skip", reason="campaign_run_cancelled")
        if campaign.status != CampaignStatus.active:
            return SmsDispatchDecision(action="defer", reason="campaign_paused", available_at=now + timedelta(seconds=30))
        if job.priority >= 100:
            snapshot = run.campaign_snapshot if run else delivery_options(campaign)
            available_at, _ = sending_interval(now, snapshot.get("sending_window"))
            if available_at > now:
                return SmsDispatchDecision(action="defer", reason="outside_sending_window", available_at=available_at)
        return SmsDispatchDecision(action="allow")

    async def sms_job_eligibility(self, job):
        """Called by the shared queue immediately before its actual SEND request."""
        from app.services.campaign_runs import delivery_options, marketing_contact_predicate
        from app.services.segments import segment_service
        from app.services.sms_queue import SmsDispatchDecision

        context = job.context_json or {}
        recipient_id = context.get("recipient_id")
        if recipient_id is None or job.operation != "send":
            return SmsDispatchDecision(action="allow")
        now = self.clock().astimezone(UTC)
        async with self.session_factory() as session:
            recipient = await session.get(MessageRecipient, recipient_id)
            if recipient is None:
                return SmsDispatchDecision(action="skip", reason="recipient_removed")
            # Match lifecycle-control lock order: campaign -> customer -> recipient.
            campaign = (await session.execute(select(Campaign).where(Campaign.id == recipient.campaign_id)
                .with_for_update(key_share=True).execution_options(populate_existing=True))).scalar_one()
            customer = (await session.execute(select(Customer).where(Customer.id == recipient.customer_id)
                .with_for_update(key_share=True).execution_options(populate_existing=True))).scalar_one_or_none()
            recipient = (await session.execute(select(MessageRecipient).where(MessageRecipient.id == recipient_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one()
            run = await session.get(CampaignRun, recipient.run_id) if recipient.run_id else None
            snapshot = run.campaign_snapshot if run else {
                **delivery_options(campaign), "purpose": campaign.purpose.value,
            }
            recipient.sms_queue_job_id = job.id
            marketing_delivery = job.priority >= 100
            reason = None
            if recipient.sent_at is not None:
                reason = "already_accepted"
            elif recipient.status != MessageDeliveryStatus.pending:
                reason = recipient.last_error or "recipient_not_pending"
            elif run and run.status == "cancelled":
                reason = "campaign_run_cancelled"
            elif campaign.status == CampaignStatus.archived:
                reason = "campaign_archived"
            elif campaign.status != CampaignStatus.active:
                await session.commit()
                return SmsDispatchDecision(action="defer", reason="campaign_paused", available_at=now + timedelta(seconds=30))
            elif recipient.send_started_at is not None:
                reason = "delivery_already_reserved"

            if reason is None:
                available_at, _ = sending_interval(now, snapshot.get("sending_window") if marketing_delivery else None)
                for scheduled in (recipient.scheduled_at, recipient.next_retry_at):
                    if scheduled is not None and scheduled > available_at:
                        available_at = scheduled
                if not marketing_delivery:
                    review_id = (await session.execute(select(ReviewRequest.id).where(ReviewRequest.recipient_id == recipient.id))).scalar_one_or_none()
                    if review_id is not None:
                        quiet_until = self.messaging.review_sms_deferred_until(campaign, MessageChannel.sms, now=now)
                        if quiet_until is not None and quiet_until > available_at:
                            available_at = quiet_until
                if available_at > now:
                    await session.commit()
                    return SmsDispatchDecision(action="defer", reason="outside_sending_window", available_at=available_at)
                quota = int(snapshot.get("sms_recipients_per_minute", settings.sms_campaign_recipients_per_minute))
                if marketing_delivery:
                    dispatch_at = func.coalesce(SmsQueueJob.transport_started_at, MessageRecipient.send_started_at)
                    count, oldest = (await session.execute(select(func.count(MessageRecipient.id), func.min(dispatch_at))
                        .select_from(MessageRecipient).outerjoin(SmsQueueJob, SmsQueueJob.id == MessageRecipient.sms_queue_job_id)
                        .where(MessageRecipient.campaign_id == campaign.id, MessageRecipient.channel == MessageChannel.sms,
                               MessageRecipient.send_started_at.is_not(None), dispatch_at > now - timedelta(seconds=60)))).one()
                    if count >= quota:
                        await session.commit()
                        return SmsDispatchDecision(action="defer", reason="campaign_throughput_limit",
                                                   available_at=max(now + timedelta(milliseconds=1), oldest + timedelta(seconds=60)))
                preference = await self.messaging.get_preference(session, recipient.customer_id)
                purpose = MessagePurpose(snapshot["purpose"])
                _, reason = self.messaging.communication_allowed(preference, purpose)
                if customer is None or not customer.is_active:
                    reason = "customer_inactive"
                if reason is None and not customer.phone:
                    reason = "channel_unreachable"
                if reason is None:
                    from app.services.sms import SmsService
                    queued_phones = (job.payload or {}).get("phone") or []
                    if queued_phones and SmsService()._smsclub_phone(customer.phone) != str(queued_phones[0]):
                        reason = "contact_destination_changed"
                if reason is None and snapshot.get("exclude_upcoming_booking"):
                    booked = (await session.execute(select(Customer.id).where(Customer.id == customer.id,
                        segment_service.upcoming_booking_predicate(now)))).scalar_one_or_none()
                    if booked is not None:
                        reason = "upcoming_booking"
                if reason is None and run and snapshot.get("exclude_returned_since_snapshot"):
                    returned = (await session.execute(select(Customer.id).where(Customer.id == customer.id,
                        segment_service.last_visit_at_expression(now) > run.evaluated_at))).scalar_one_or_none()
                    if returned is not None:
                        reason = "returned_since_snapshot"
                if reason is None and marketing_delivery and purpose == MessagePurpose.marketing:
                    capped = (await session.execute(select(marketing_contact_predicate(customer.id, now,
                        snapshot.get("marketing_frequency_days", 7), exclude_recipient_id=recipient.id)))).scalar_one()
                    if capped:
                        reason = "marketing_frequency_cap"
            if reason is not None:
                await session.commit()
                return SmsDispatchDecision(action="skip", reason=reason)
            recipient.channel = MessageChannel.sms
            recipient.send_started_at = now
            await session.commit()
            return SmsDispatchDecision(action="allow")

    async def sms_job_outcome(self, job) -> None:
        """Idempotently project durable queue results, including safe retry deferral."""
        from app.services.campaign_runs import CampaignRunService
        recipient_id = (job.context_json or {}).get("recipient_id")
        if recipient_id is None or job.operation != "send":
            return
        async with self.session_factory() as session:
            recipient = (await session.execute(select(MessageRecipient).where(MessageRecipient.id == recipient_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if recipient is None:
                return
            recipient.sms_queue_job_id = job.id
            receipt_update = job.status == "delivered" or (job.status == "failed" and job.error_code == "provider_delivery_failed")
            if recipient.sent_at is not None and not receipt_update:
                # Delivery-receipt updates may already have advanced sent -> delivered
                # or failed; a retried queue projection must never overwrite them.
                await session.commit()
                return
            if recipient.status == MessageDeliveryStatus.delivered:
                await session.commit()
                return
            error_code = job.error_code or job.status
            if job.status == "queued":
                if recipient.status == MessageDeliveryStatus.skipped:
                    await session.commit()
                    return
                target_status = MessageDeliveryStatus.pending
                reason = f"sms_retry_queued:{error_code}"
                recipient.send_started_at = None
                recipient.next_retry_at = job.available_at
            elif job.status == "accepted":
                target_status = MessageDeliveryStatus.sent
                reason = None
                recipient.sent_at = getattr(job, "accepted_at", None) or self.clock()
                recipient.provider_message_id = job.provider_message_id
                recipient.next_retry_at = None
            elif job.status == "delivered":
                target_status = MessageDeliveryStatus.delivered
                reason = None
                recipient.sent_at = recipient.sent_at or getattr(job, "accepted_at", None) or self.clock()
                recipient.delivered_at = getattr(job, "delivered_at", None) or self.clock()
                recipient.provider_message_id = job.provider_message_id
                recipient.next_retry_at = None
            elif job.status in {"cancelled", "skipped"}:
                target_status = MessageDeliveryStatus.skipped
                reason = error_code
                recipient.next_retry_at = None
            elif job.status in {"failed", "uncertain"}:
                target_status = MessageDeliveryStatus.failed
                reason = f"delivery_uncertain:{error_code}" if job.status == "uncertain" else error_code
                recipient.next_retry_at = None
                if job.error_code == "provider_delivery_failed":
                    recipient.sent_at = recipient.sent_at or getattr(job, "accepted_at", None)
                    recipient.provider_message_id = job.provider_message_id
            else:
                await session.commit()
                return
            changed = (recipient.status != target_status or recipient.last_error != reason or recipient.attempts != job.attempts)
            recipient.status = target_status
            recipient.last_error = reason
            recipient.attempts = job.attempts
            if changed:
                session.add(self.messaging._log_from_recipient(recipient, target_status,
                    error_reason=reason, provider_response={"sms_queue_job_id": job.id,
                        "provider_message_id": job.provider_message_id, "queue_status": job.status}))
            if job.priority < 100:
                if target_status == MessageDeliveryStatus.sent:
                    await self.messaging.mark_review_request_sent(session, recipient)
                elif target_status in {MessageDeliveryStatus.failed, MessageDeliveryStatus.skipped, MessageDeliveryStatus.delivered}:
                    review = (await session.execute(select(ReviewRequest).where(ReviewRequest.recipient_id == recipient.id)
                        .options(selectinload(ReviewRequest.events)))).scalar_one_or_none()
                    if review is not None and review.status not in {ReviewRequestStatus.submitted, ReviewRequestStatus.expired}:
                        from app.services.master_reviews import master_review_service
                        if target_status == MessageDeliveryStatus.delivered:
                            review.sent_at = recipient.sent_at
                            review.delivered_at = recipient.delivered_at
                        master_review_service.transition_request(review,
                            ReviewRequestStatus.delivered if target_status == MessageDeliveryStatus.delivered else ReviewRequestStatus.failed,
                            channel=MessageChannel.sms, reason=reason)
            run = await session.get(CampaignRun, recipient.run_id) if recipient.run_id else None
            await CampaignRunService._complete_run_if_finished(session, run)
            await session.commit()

    async def progress(self, session, campaign: Campaign, *, run: CampaignRun | None = None) -> dict[str, Any]:
        from app.services.campaign_runs import delivery_options
        predicates = [MessageRecipient.campaign_id == campaign.id]
        if run is not None:
            predicates.append(MessageRecipient.run_id == run.id)
        normalized = case(
            (MessageRecipient.status == MessageDeliveryStatus.delivered, "delivered"),
            (SmsQueueJob.status == "delivered", "delivered"),
            (SmsQueueJob.status == "uncertain", "uncertain"),
            (MessageRecipient.last_error.like("delivery_uncertain%"), "uncertain"),
            (SmsQueueJob.status.in_(("failed",)), "failed"),
            (SmsQueueJob.status.in_(("skipped", "cancelled")), "skipped"),
            (MessageRecipient.status == MessageDeliveryStatus.failed, "failed"),
            (MessageRecipient.status == MessageDeliveryStatus.skipped, "skipped"),
            (MessageRecipient.status == MessageDeliveryStatus.sent, "accepted"),
            (SmsQueueJob.status == "accepted", "accepted"),
            else_="queued",
        )
        rows = (await session.execute(select(normalized.label("state"), func.count(MessageRecipient.id))
            .select_from(MessageRecipient).outerjoin(SmsQueueJob, SmsQueueJob.id == MessageRecipient.sms_queue_job_id)
            .where(*predicates).group_by(normalized))).all()
        counts = dict.fromkeys(("queued", "accepted", "delivered", "failed", "skipped", "uncertain"), 0)
        counts.update({state: count for state, count in rows})
        dispatching = (await session.execute(select(func.count(MessageRecipient.id))
            .select_from(MessageRecipient).join(SmsQueueJob, SmsQueueJob.id == MessageRecipient.sms_queue_job_id)
            .where(*predicates, SmsQueueJob.status == "dispatching"))).scalar_one()
        snapshot = run.campaign_snapshot if run and run.campaign_snapshot else delivery_options(campaign)
        mixed_runs = False
        if run is None and counts["queued"]:
            pending_runs = list((await session.execute(select(MessageRecipient.run_id).where(
                *predicates, MessageRecipient.status == MessageDeliveryStatus.pending,
            ).distinct().limit(2))).scalars())
            mixed_runs = len(pending_runs) > 1
            if len(pending_runs) == 1 and pending_runs[0] is not None:
                only_run = await session.get(CampaignRun, pending_runs[0])
                if only_run and only_run.campaign_snapshot:
                    snapshot = only_run.campaign_snapshot
        quota = int(snapshot.get("sms_recipients_per_minute", settings.sms_campaign_recipients_per_minute))
        paused = campaign.status != CampaignStatus.active
        cancelled = bool(run and run.status == "cancelled") or campaign.status == CampaignStatus.archived
        now = self.clock().astimezone(UTC)
        window_start, _ = sending_interval(now, snapshot.get("sending_window"))
        finish = None
        estimate_note = "Estimate includes current campaign pacing, account limits and known queued work; incoming priority traffic can extend it."
        if mixed_runs:
            estimate_note = "Inspect individual runs for an ETA; this campaign has multiple pending run policies."
        elif not paused and not cancelled:
            if not counts["queued"]:
                finish = now
            else:
                start = max(now, window_start)
                account = await session.get(SmsAccountThrottle, settings.sms_club_account_key)
                if account is not None:
                    start = max([start, *(value for value in (account.cooldown_until, account.next_request_at) if value)])
                available_at = (await session.execute(select(func.min(func.coalesce(
                    SmsQueueJob.available_at, MessageRecipient.next_retry_at, MessageRecipient.scheduled_at)))
                    .select_from(MessageRecipient).outerjoin(SmsQueueJob, SmsQueueJob.id == MessageRecipient.sms_queue_job_id)
                    .where(*predicates, MessageRecipient.status == MessageDeliveryStatus.pending))).scalar_one()
                if available_at is not None:
                    start = max(start, available_at)
                dispatch_at = func.coalesce(SmsQueueJob.transport_started_at, MessageRecipient.send_started_at)
                used, oldest = (await session.execute(select(func.count(MessageRecipient.id), func.min(dispatch_at))
                    .select_from(MessageRecipient).outerjoin(SmsQueueJob, SmsQueueJob.id == MessageRecipient.sms_queue_job_id)
                    .where(MessageRecipient.campaign_id == campaign.id, MessageRecipient.channel == MessageChannel.sms,
                           MessageRecipient.send_started_at.is_not(None), dispatch_at > now - timedelta(seconds=60)))).one()
                if used and counts["queued"] > max(0, quota - used):
                    start = max(start, oldest + timedelta(seconds=60))
                competing = (await session.execute(select(func.count(SmsQueueJob.id)).where(
                    SmsQueueJob.account_key == settings.sms_club_account_key,
                    SmsQueueJob.status.in_(("queued", "dispatching")), SmsQueueJob.priority <= 100,
                ))).scalar_one()
                rate = min(quota / 60, settings.sms_club_requests_per_second)
                duration = max(counts["queued"] / rate, competing / settings.sms_club_requests_per_second)
                finish = estimated_finish(start, duration, snapshot.get("sending_window"))
        return {
            "total": sum(counts.values()), "counts": counts, "dispatching": dispatching,
            "paused": paused, "cancelled": cancelled, "sms_recipients_per_minute": quota,
            "estimated_remaining_seconds": ceil((finish - now).total_seconds()) if finish else None,
            "estimated_completion_at": finish,
            "next_window_at": window_start if window_start > now and counts["queued"] else None,
            "estimate_note": estimate_note,
        }



campaign_dispatch_service = CampaignDispatchService()
