"""Durable, account-wide SMSClub admission with bounded retries.

The PostgreSQL account row serializes admission across every API and worker.
Provider credentials stay in process configuration; only request data is queued.
Send attempts with uncertain acceptance are never automatically repeated.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.sms_queue import SmsAccountThrottle, SmsQueueJob

logger = logging.getLogger(__name__)
TERMINAL_STATES = frozenset({"accepted", "delivered", "failed", "skipped", "uncertain", "cancelled"})


@dataclass(frozen=True)
class SmsRequestContext:
    recipient_id: int | None = None
    run_id: int | None = None
    campaign_id: int | None = None
    customer_id: int | None = None
    waitlist_offer_id: int | None = None
    safe_body: str | None = None
    priority: int = 10
    idempotency_key: str | None = None
    enqueue_only: bool = False


sms_request_context: ContextVar[SmsRequestContext | None] = ContextVar("sms_request_context", default=None)


@contextmanager
def use_sms_context(context: SmsRequestContext | None = None, **kwargs):
    token = sms_request_context.set(context or SmsRequestContext(**kwargs))
    try:
        yield
    finally:
        sms_request_context.reset(token)


@dataclass(frozen=True)
class SmsDispatchDecision:
    action: str = "allow"
    reason: str | None = None
    available_at: datetime | None = None


class SmsTransportError(HTTPException):
    def __init__(self, status_code: int, detail: str, *, code: str, retryable: bool = False,
                 ambiguous: bool = False, retry_after_seconds: float | None = None):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.retry_after_seconds = retry_after_seconds


class SmsQueueError(HTTPException):
    def __init__(self, job: SmsQueueJob, status_code: int = 503):
        super().__init__(status_code=status_code, detail={
            "code": job.error_code or job.status, "job_id": job.id,
            "message": job.error_detail or "SMS operation is pending",
        })
        self.job_id = job.id


class SmsQueuePending(SmsQueueError):
    pass


class SmsQueueUncertain(SmsQueueError):
    pass


class SmsQueueSkipped(SmsQueueError):
    def __init__(self, job: SmsQueueJob):
        super().__init__(job, 409)


class SmsQueuePermanentError(SmsQueueError):
    def __init__(self, job: SmsQueueJob):
        super().__init__(job, 400)


class SmsQueueCancelled(SmsQueueSkipped):
    pass


def retry_delay(attempts: int, retry_after: float | None = None) -> float:
    """Bound exponential jitter; an explicit Retry-After is a lower bound."""
    ceiling = min(settings.sms_queue_retry_max_seconds,
                  settings.sms_queue_retry_base_seconds * (2 ** max(0, attempts - 1)))
    jittered = random.uniform(ceiling / 2, ceiling)
    return max(jittered, max(0, retry_after or 0))


class SmsQueueService:
    def __init__(self, session_factory=None, transport=None, clock=None,
                 before_dispatch=None, after_outcome=None):
        self.session_factory = session_factory or AsyncSessionLocal
        self.transport = transport
        self.clock = clock
        self.before_dispatch = before_dispatch
        self.after_outcome = after_outcome

    @staticmethod
    def idempotency_key(raw_key: str) -> str:
        return f"{settings.sms_club_account_key}:{hashlib.sha256(str(raw_key).encode()).hexdigest()}"

    async def find_by_key(self, raw_key: str) -> SmsQueueJob | None:
        async with self.session_factory() as session:
            return (await session.scalars(select(SmsQueueJob).where(
                SmsQueueJob.idempotency_key == self.idempotency_key(raw_key),
            ))).first()

    async def _now(self, session) -> datetime:
        value = await self.clock(session) if self.clock else await session.scalar(select(func.clock_timestamp()))
        if value.utcoffset() is None:
            raise ValueError("SMS queue clock must return timezone-aware values")
        return value.astimezone(UTC)

    async def enqueue(self, operation: str, payload: dict, *, priority: int = 10,
                      context: dict | SmsRequestContext | None = None,
                      idempotency_key: str | None = None, expires_at: datetime | None = None,
                      external_session=None) -> SmsQueueJob:
        if operation not in {"send", "status"}:
            raise ValueError("Unsupported SMSClub queue operation")
        allowed_payload = {"phone", "message", "src_addr", "lifetime"} if operation == "send" else {"id_sms"}
        if set(payload) - allowed_payload:
            raise ValueError("Unsupported SMSClub payload fields")
        numbers = payload.get("phone", []) if operation == "send" else payload.get("id_sms", [])
        if not isinstance(numbers, list) or not 1 <= len(numbers) <= 100:
            raise ValueError("SMSClub operations require between 1 and 100 recipients or message IDs")
        if operation == "send" and len(numbers) != 1:
            # Business delivery is one durable recipient, allowing independent
            # eligibility, retries, and results; API maximum remains 100.
            raise ValueError("Queue send jobs contain exactly one recipient")
        if not 0 <= priority <= 1000:
            raise ValueError("Invalid SMS queue priority")
        context_data = asdict(context) if isinstance(context, SmsRequestContext) else dict(context or {})
        if set(context_data) - set(SmsRequestContext.__dataclass_fields__):
            raise ValueError("Unsupported SMS queue context fields")
        context_data = {key: value for key, value in context_data.items() if value is not None}
        raw_key = idempotency_key or context_data.get("idempotency_key")
        if raw_key is None and context_data.get("recipient_id") is not None and operation == "send":
            raw_key = f"recipient:{context_data['recipient_id']}:sms"
        raw_key = raw_key or str(uuid4())
        account = settings.sms_club_account_key
        key = self.idempotency_key(str(raw_key))
        async def persist(session, *, commit: bool) -> SmsQueueJob:
            now = await self._now(session)
            expiry = expires_at or now + timedelta(minutes=settings.sms_queue_ttl_minutes)
            if expiry.utcoffset() is None:
                raise ValueError("SMS expiry must include a timezone offset")
            values = dict(id=str(uuid4()), account_key=account, idempotency_key=key, operation=operation,
                          priority=priority, payload=dict(payload), context_json=context_data,
                          status="queued", attempts=0, available_at=now, expires_at=expiry)
            await session.execute(insert(SmsQueueJob).values(**values).on_conflict_do_nothing(
                index_elements=[SmsQueueJob.idempotency_key]
            ))
            if commit:
                await session.commit()
            else:
                await session.flush()
            return (await session.scalars(select(SmsQueueJob).where(SmsQueueJob.idempotency_key == key))).one()
        if external_session is not None:
            return await persist(external_session, commit=False)
        async with self.session_factory() as session:
            return await persist(session, commit=True)

    async def get(self, job_id: str) -> SmsQueueJob | None:
        async with self.session_factory() as session:
            return await session.get(SmsQueueJob, job_id)

    async def request(self, operation: str, payload: dict, **kwargs) -> dict:
        job = await self.enqueue(operation, payload, **kwargs)
        deadline = asyncio.get_running_loop().time() + settings.sms_queue_wait_seconds
        while True:
            job = await self.get(job.id)
            if job.status in {"accepted", "delivered"}:
                return job.result_json or {}
            if job.status == "uncertain":
                raise SmsQueueUncertain(job)
            if job.status == "skipped":
                raise SmsQueueSkipped(job)
            if job.status == "cancelled":
                raise SmsQueueCancelled(job)
            if job.status == "failed":
                raise SmsQueuePermanentError(job)
            if asyncio.get_running_loop().time() >= deadline:
                raise SmsQueuePending(job)
            progressed = await self.process_one()
            if not progressed:
                await asyncio.sleep(settings.sms_queue_poll_seconds)

    async def _account(self, session) -> SmsAccountThrottle:
        await session.execute(insert(SmsAccountThrottle).values(account_key=settings.sms_club_account_key)
                              .on_conflict_do_nothing(index_elements=[SmsAccountThrottle.account_key]))
        return (await session.scalars(select(SmsAccountThrottle)
                .where(SmsAccountThrottle.account_key == settings.sms_club_account_key)
                .with_for_update())).one()

    @staticmethod
    def _clear_payload(job: SmsQueueJob) -> None:
        # Preserve neither OTPs nor personalized marketing/service bodies once
        # the operation is terminal, including uncertain and cancelled sends.
        job.payload = {}

    async def _recover(self, session, now: datetime) -> list[str]:
        stale = list((await session.scalars(select(SmsQueueJob).where(
            SmsQueueJob.account_key == settings.sms_club_account_key,
            or_(
                (SmsQueueJob.status == "dispatching") & (SmsQueueJob.lease_expires_at <= now),
                (SmsQueueJob.status == "queued") & (SmsQueueJob.expires_at <= now),
            ),
        ).order_by(SmsQueueJob.priority, SmsQueueJob.available_at).limit(50).with_for_update(skip_locked=True))).all())
        for job in stale:
            if job.status == "dispatching" and job.transport_started_at is None:
                job.attempts = max(0, job.attempts - 1)
            if job.status == "dispatching" and job.operation == "send" and job.transport_started_at is not None:
                job.status, job.error_code = "uncertain", "worker_interrupted"
                job.error_detail = "Worker lease expired after dispatch was reserved; automatic resend is disabled"
            elif job.expires_at is not None and job.expires_at <= now:
                job.status, job.error_code = "cancelled", "expired"
                job.error_detail = "Queued operation expired before completion"
            elif job.attempts >= settings.sms_queue_max_attempts:
                job.status, job.error_code = "failed", "retry_exhausted"
                job.error_detail = "Status retry budget exhausted"
            else:
                job.status, job.error_code = "queued", "worker_interrupted"
                job.available_at = now
            job.lease_token = None
            job.lease_expires_at = None
            job.outcome_projected_at = None
            if job.status in TERMINAL_STATES:
                self._clear_payload(job)
        return [job.id for job in stale]

    async def _project(self, job_id: str) -> bool:
        async with self.session_factory() as session:
            # Serialize callbacks without holding the queue row while callback
            # code locks business rows. In particular, an old retry callback
            # must not clear a newer attempt's send reservation.
            lock_key = int.from_bytes(hashlib.sha256(job_id.encode()).digest()[:8], "big", signed=True)
            locked = await session.scalar(select(func.pg_try_advisory_xact_lock(lock_key)))
            if not locked:
                return False
            job = await session.get(SmsQueueJob, job_id)
            if job is None or job.outcome_projected_at is not None or job.status == "dispatching":
                return False
            if job.status == "queued" and not job.error_code:
                return False
            expected_status, expected_attempts = job.status, job.attempts
            try:
                if self.after_outcome is not None:
                    await self.after_outcome(job)
                elif job.context_json.get("waitlist_offer_id") is not None:
                    from app.services.sms_notification_queue import sms_job_outcome
                    await sms_job_outcome(job)
                elif job.context_json.get("recipient_id") is not None:
                    from app.services.campaign_runs import sms_job_outcome
                    await sms_job_outcome(job)
            except Exception as exc:
                logger.warning("SMS job outcome projection failed",
                               extra={"sms_job_id": job.id, "error_type": type(exc).__name__})
                return False
            current = await session.get(SmsQueueJob, job.id, with_for_update=True, populate_existing=True)
            if current.status == expected_status and current.attempts == expected_attempts:
                current.outcome_projected_at = await self._now(session)
                await session.commit()
        return True

    async def process(self, limit: int | None = None) -> int:
        processed = 0
        for _ in range(limit or settings.sms_queue_batch_size):
            if not await self.process_one():
                break
            processed += 1
        return processed

    async def process_one(self) -> bool:
        job = None
        recovered = []
        async with self.session_factory() as session:
            account = await self._account(session)
            now = await self._now(session)
            recovered = await self._recover(session, now)
            ready_at = max((value for value in (account.next_request_at, account.cooldown_until) if value), default=now)
            active = await session.scalar(select(func.count()).select_from(SmsQueueJob).where(
                SmsQueueJob.account_key == account.account_key, SmsQueueJob.status == "dispatching",
            ))
            if ready_at <= now and active < settings.sms_queue_concurrency:
                job = (await session.scalars(select(SmsQueueJob).where(
                    SmsQueueJob.account_key == account.account_key, SmsQueueJob.status == "queued",
                    SmsQueueJob.available_at <= now,
                    or_(SmsQueueJob.error_code.is_(None), SmsQueueJob.outcome_projected_at.is_not(None)),
                    or_(SmsQueueJob.expires_at.is_(None), SmsQueueJob.expires_at > now),
                ).order_by(SmsQueueJob.priority, SmsQueueJob.available_at, SmsQueueJob.created_at, SmsQueueJob.id)
                    .limit(1).with_for_update(skip_locked=True))).first()
                if job is not None:
                    job.status = "dispatching"
                    job.claimed_at = now
                    job.transport_started_at = None
                    job.lease_expires_at = now + timedelta(seconds=settings.sms_queue_lease_seconds)
                    job.lease_token = str(uuid4())
                    job.attempts += 1
                    job.error_code = job.error_detail = None
                    job.outcome_projected_at = None
            await session.commit()
        for job_id in recovered:
            await self._project(job_id)
        if job is None:
            # Outcomes are durable independently of business projections. Retry
            # a bounded projection after a worker crash without resending SMS.
            async with self.session_factory() as session:
                pending = (await session.scalars(select(SmsQueueJob.id).where(
                    SmsQueueJob.account_key == settings.sms_club_account_key,
                    SmsQueueJob.outcome_projected_at.is_(None),
                    or_(SmsQueueJob.status.in_(TERMINAL_STATES),
                        (SmsQueueJob.status == "queued") & SmsQueueJob.error_code.is_not(None)),
                ).order_by(SmsQueueJob.updated_at).limit(1))).first()
            return bool(await self._project(pending)) if pending else bool(recovered)
        decision = SmsDispatchDecision()
        try:
            if self.before_dispatch is not None:
                decision = await self.before_dispatch(job)
            elif job.context_json.get("waitlist_offer_id") is not None:
                from app.services.sms_notification_queue import sms_job_eligibility
                decision = await sms_job_eligibility(job)
            elif job.context_json.get("recipient_id") is not None:
                from app.services.campaign_runs import sms_job_eligibility
                decision = await sms_job_eligibility(job)
            if decision.action != "allow":
                if decision.action not in {"defer", "skip"}:
                    raise ValueError("Unknown SMS eligibility decision")
                await self._finish(job, decision=decision)
                return True
        except Exception as exc:
            # No provider request has begun, so callback failures are safely
            # deferred and cannot turn a temporary DB failure into a resend.
            logger.warning("SMS dispatch eligibility unavailable",
                           extra={"sms_job_id": job.id, "error_type": type(exc).__name__})
            await self._finish(job, decision=SmsDispatchDecision("defer", "eligibility_unavailable"))
            return True
        try:
            admission = await self._admit_transport(job)
            if admission is not None:
                await self._finish(job, decision=admission)
                return True
            if self.transport is not None:
                result = await self.transport(job)
            else:
                from app.services.sms import SmsService
                result = await SmsService()._execute_queue_job(job)
        except SmsTransportError as exc:
            await self._finish(job, error=exc)
        except Exception:
            # Unexpected failures after transport admission have uncertain send
            # acceptance. Read-only status calls can safely be attempted again.
            await self._finish(job, error=SmsTransportError(
                503, "SMS transport failed without a definitive provider outcome", code="transport_uncertain",
                ambiguous=job.operation == "send", retryable=job.operation == "status",
            ))
        else:
            await self._finish(job, result=result)
        return True

    async def _admit_transport(self, claimed: SmsQueueJob) -> SmsDispatchDecision | None:
        """Reserve no future slots: gate again after potentially slow eligibility."""
        async with self.session_factory() as session:
            if claimed.context_json.get("recipient_id") is not None:
                from app.models.messaging import Campaign, MessageRecipient
                campaign_id = claimed.context_json.get("campaign_id")
                if campaign_id is None:
                    campaign_id = await session.scalar(select(MessageRecipient.campaign_id).where(
                        MessageRecipient.id == claimed.context_json["recipient_id"],
                    ))
                await session.scalar(select(Campaign).where(Campaign.id == campaign_id)
                                     .with_for_update(key_share=True))
            account = await self._account(session)
            now = await self._now(session)
            job = await session.get(SmsQueueJob, claimed.id, with_for_update=True)
            if (job.status != "dispatching" or job.lease_token != claimed.lease_token
                    or job.lease_expires_at <= now):
                return SmsDispatchDecision("defer", "claim_expired", now)
            if job.context_json.get("recipient_id") is not None:
                from app.services.campaign_dispatch import campaign_dispatch_service
                if job.context_json.get("campaign_id") is None and campaign_id is not None:
                    job.context_json = {**job.context_json, "campaign_id": campaign_id}
                final_decision = await campaign_dispatch_service.final_gate(session, job, now)
                if final_decision.action != "allow":
                    return final_decision
            ready_at = max((value for value in (account.next_request_at, account.cooldown_until) if value), default=now)
            if ready_at > now:
                return SmsDispatchDecision("defer", "account_throttled", ready_at)
            # Claim order alone cannot guarantee priority after asynchronous
            # eligibility checks. A slower urgent claim must retain precedence
            # over lower-priority work until its transport has been admitted.
            higher_priority = await session.scalar(select(SmsQueueJob.id).where(
                SmsQueueJob.account_key == job.account_key,
                SmsQueueJob.priority < job.priority,
                or_(SmsQueueJob.expires_at.is_(None), SmsQueueJob.expires_at > now),
                or_(
                    (SmsQueueJob.status == "queued") & (SmsQueueJob.available_at <= now)
                    & or_(SmsQueueJob.error_code.is_(None), SmsQueueJob.outcome_projected_at.is_not(None)),
                    (SmsQueueJob.status == "dispatching") & SmsQueueJob.transport_started_at.is_(None)
                    & (SmsQueueJob.lease_expires_at > now),
                ),
            ).limit(1))
            if higher_priority is not None:
                return SmsDispatchDecision("defer", "priority_wait",
                                           now + timedelta(seconds=settings.sms_queue_poll_seconds))
            account.next_request_at = now + timedelta(seconds=1 / settings.sms_club_requests_per_second)
            job.transport_started_at = now
            await session.commit()
            claimed.transport_started_at = now
            return None

    async def _finish(self, claimed: SmsQueueJob, *, result: dict | None = None,
                      error: SmsTransportError | None = None, decision: SmsDispatchDecision | None = None) -> None:
        delivery_updates = []
        async with self.session_factory() as session:
            account = await self._account(session)
            now = await self._now(session)
            job = await session.get(SmsQueueJob, claimed.id, with_for_update=True)
            if job.status != "dispatching" or job.lease_token != claimed.lease_token:
                return
            job.lease_token = None
            job.lease_expires_at = None
            if decision is not None:
                job.attempts = max(0, job.attempts - 1)
                job.status = "queued" if decision.action == "defer" else "skipped"
                job.error_code = decision.reason or decision.action
                job.error_detail = "Dispatch deferred" if decision.action == "defer" else "Delivery eligibility rejected"
                job.available_at = decision.available_at or now + timedelta(seconds=settings.sms_queue_retry_base_seconds)
            elif error is not None:
                job.error_code, job.error_detail = error.code, str(error.detail)
                delay = retry_delay(job.attempts, error.retry_after_seconds)
                if error.code == "rate_limited" or error.retry_after_seconds is not None:
                    account.cooldown_until = max(account.cooldown_until or now, now + timedelta(seconds=delay))
                if error.ambiguous and job.operation == "send":
                    job.status = "uncertain"
                elif error.retryable and job.attempts < settings.sms_queue_max_attempts:
                    job.available_at = now + timedelta(seconds=delay)
                    job.status = "queued"
                else:
                    job.status = "failed"
            else:
                job.status = "accepted"
                job.result_json = result or {}
                if job.operation == "send":
                    info = (result or {}).get("success_request", {}).get("info", {})
                    job.provider_message_id = str(next(iter(info))) if isinstance(info, dict) and info else None
                    job.accepted_at = now
                else:
                    statuses = (result or {}).get("success_request", {}).get("info", {})
                    if isinstance(statuses, dict) and statuses:
                        sends = list((await session.scalars(select(SmsQueueJob).where(
                            SmsQueueJob.account_key == job.account_key,
                            SmsQueueJob.operation == "send", SmsQueueJob.provider_message_id.in_(statuses),
                            SmsQueueJob.accepted_at.is_not(None),
                        ).with_for_update())).all())
                        for sent_job in sends:
                            receipt = statuses.get(sent_job.provider_message_id)
                            if sent_job.status == "delivered" or receipt == sent_job.delivery_status:
                                continue
                            if receipt == "DELIVRD":
                                sent_job.status = "delivered"
                                sent_job.delivered_at = now
                                sent_job.error_code = sent_job.error_detail = None
                            elif receipt in {"EXPIRED", "UNDELIV", "REJECTD"}:
                                sent_job.status = "failed"
                                sent_job.error_code = "provider_delivery_failed"
                                sent_job.error_detail = f"Provider reported {receipt} after accepting the message"
                            elif receipt == "ENROUTE" and sent_job.status == "accepted":
                                sent_job.delivery_status = receipt
                                continue
                            else:
                                continue
                            sent_job.delivery_status = receipt
                            sent_job.outcome_projected_at = None
                            delivery_updates.append(sent_job.id)
            if decision is None:
                # Spacing measured again after transport is deliberately
                # conservative even when a worker is delayed before emission.
                account.next_request_at = max(account.next_request_at or now,
                                              now + timedelta(seconds=1 / settings.sms_club_requests_per_second))
            job.outcome_projected_at = None
            if job.status in TERMINAL_STATES:
                self._clear_payload(job)
            await session.commit()
        await self._project(claimed.id)
        for job_id in delivery_updates:
            await self._project(job_id)
