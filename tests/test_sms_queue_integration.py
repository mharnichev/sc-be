"""Durable SMS queue tests against explicitly isolated PostgreSQL only.

The transport records SMSClub HTTP requests in memory. Queue persistence, worker
claims, shared account throttling, and response parsing remain real.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.core.config import settings
from app.models.campaign_run import CampaignRun
from app.models.customer import Customer
from app.models.messaging import Campaign, MessageChannel, MessageDeliveryStatus, MessageRecipient
from app.services.sms import SmsService
from test_segments_integration import (
    KYIV, add_booking, add_campaign, add_customer, add_segment, anyio_backend, database,
)


class RecordingSmsClub:
    """Fake the last HTTP boundary, not queue/limiter behavior."""

    def __init__(self, outcome=None):
        self.requests = []
        self._lock = threading.Lock()
        self.outcome = outcome
        self.latency = 0
        self.in_flight = 0
        self.max_in_flight = 0

    def post_json(self, url, payload, headers):
        with self._lock:
            ordinal = len(self.requests) + 1
            self.requests.append({"ordinal": ordinal, "at": time.monotonic(), "url": url, "payload": payload})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency:
                time.sleep(self.latency)
            if self.outcome is not None:
                result = self.outcome(ordinal, url, payload)
                if result is not None:
                    return result
            if url.endswith("/sms/status"):
                return {"success_request": {"info": {str(message_id): "DELIVRD" for message_id in payload["id_sms"]}}}
            assert len(payload["phone"]) == 1, "Personalized messages must preserve independent recipient outcomes"
            return {"success_request": {"info": {f"sandbox-{ordinal}": payload["phone"][0]}}}
        finally:
            with self._lock:
                self.in_flight -= 1


@pytest.fixture
def sms_transport(monkeypatch):
    transport = RecordingSmsClub()
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "sandbox-account-never-used-on-network")
    monkeypatch.setattr(SmsService, "_post_json", lambda self, url, payload, headers: transport.post_json(url, payload, headers))
    return transport


async def seed_campaign_run(database, *, count=1, suffix=0, **campaign_options):
    """Create a real draft and snapshot without dispatching providers."""
    from app.services.campaign_runs import CampaignRunService
    async with database() as session:
        customers = [Customer(phone=f"+38050{suffix:02}{number:05}", name=f"Sandbox {suffix}-{number}",
                              imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180)) for number in range(count)]
        session.add_all(customers)
        await session.flush()
        segment = await add_segment(session, f"SMS sandbox {suffix}")
        campaign = await add_campaign(session, [segment], **campaign_options)
        await session.commit()
        run = await CampaignRunService().launch(session, campaign, idempotency_key=f"sms-sandbox-{suffix}")
        return campaign.id, run.id, [customer.id for customer in customers]


class SimulatedClock:
    def __init__(self):
        self.value = datetime.now(UTC)

    async def __call__(self, _session):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def sms_payload(number, body=None):
    return {"phone": [f"380500{number:06}"], "message": body or f"Sandbox customer {number}", "src_addr": "Soul Cuts"}


def campaign_worker(database, clock):
    from app.services.campaign_dispatch import CampaignDispatchService
    from app.services.campaign_runs import CampaignRunService
    from app.services.messaging import MessagingService, SmsMessageProvider
    from app.services.sms_queue import SmsQueueService
    dispatch = CampaignDispatchService(session_factory=database, clock=lambda: clock.value)
    queue = SmsQueueService(session_factory=database, clock=clock,
                           before_dispatch=dispatch.sms_job_eligibility, after_outcome=dispatch.sms_job_outcome)
    messaging = MessagingService(providers={MessageChannel.sms: SmsMessageProvider(SmsService(queue=queue))})
    return CampaignRunService(messaging), queue, dispatch


@pytest.mark.anyio
async def test_pause_resume_and_cancel_unsent_preserve_already_accepted_delivery(database, sms_transport):
    from app.models.messaging import CampaignStatus
    campaign_id, run_id, _ = await seed_campaign_run(database, count=3)
    clock = SimulatedClock()
    runs, queue, dispatch = campaign_worker(database, clock)
    async with database() as session:
        await runs.process_run_messages(session)
        campaign = await session.get(Campaign, campaign_id)
        campaign.status = CampaignStatus.paused
        await session.commit()
    await queue.process_one()
    assert sms_transport.requests == []
    async with database() as session:
        campaign = await session.get(Campaign, campaign_id)
        run = await session.get(CampaignRun, run_id)
        assert (await dispatch.progress(session, campaign, run=run))["paused"] is True
        campaign.status = CampaignStatus.active
        await session.commit()
    clock.advance(31)
    await queue.process_one()
    assert len(sms_transport.requests) == 1
    async with database() as session:
        run = await session.get(CampaignRun, run_id)
        assert await runs.cancel_run_unsent(session, run) == 2
    clock.advance(60)
    await queue.process(limit=10)
    assert len(sms_transport.requests) == 1
    async with database() as session:
        recipients = list(await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id)))
        assert sum(row.status == MessageDeliveryStatus.sent for row in recipients) == 1
        assert sum(row.status == MessageDeliveryStatus.skipped for row in recipients) == 2


@pytest.mark.anyio
async def test_campaign_recipient_pace_is_independent_of_global_http_rate(database, sms_transport):
    campaign_id, run_id, _ = await seed_campaign_run(database, count=3, sms_recipients_per_minute=2)
    clock = SimulatedClock()
    runs, queue, dispatch = campaign_worker(database, clock)
    async with database() as session:
        await runs.process_run_messages(session)
    for _ in range(3):
        await queue.process_one()
        clock.advance(0.2)
    assert len(sms_transport.requests) == 2
    async with database() as session:
        campaign = await session.get(Campaign, campaign_id)
        run = await session.get(CampaignRun, run_id)
        progress = await dispatch.progress(session, campaign, run=run)
        assert progress["counts"]["queued"] == 1
        assert progress["sms_recipients_per_minute"] == 2
    clock.advance(61)
    await queue.process_one()
    assert len(sms_transport.requests) == 3


@pytest.mark.anyio
async def test_queued_campaign_defers_outside_kyiv_window_and_resumes_next_window(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    hour = datetime.now(KYIV).hour
    window = {"start": f"{hour:02}:00", "end": f"{(hour+1)%24:02}:00", "days": list(range(7))}
    await seed_campaign_run(database, sending_window=window)
    clock = SimulatedClock()
    runs, queue, _ = campaign_worker(database, clock)
    async with database() as session:
        await runs.process_run_messages(session)
    clock.advance(2*3600)
    await queue.process_one()
    assert sms_transport.requests == []
    async with database() as session:
        job = (await session.scalars(select(SmsQueueJob))).one()
        assert job.status == "queued" and job.error_code == "outside_sending_window"
        assert job.available_at > clock.value
        assert job.available_at.astimezone(KYIV).strftime("%H:%M") == window["start"]
        clock.value = job.available_at+timedelta(milliseconds=1)
    await queue.process_one()
    assert len(sms_transport.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["consent", "upcoming", "returned"])
async def test_final_dispatch_rechecks_changes_after_durable_enqueue(database, sms_transport, change):
    from app.models.booking import BookingStatus
    from app.models.messaging import ClientCommunicationPreference, ConsentStatus
    options = {"exclude_upcoming_booking": True, "exclude_returned_since_snapshot": True}
    _, run_id, customer_ids = await seed_campaign_run(database, **options)
    clock = SimulatedClock()
    runs, queue, _ = campaign_worker(database, clock)
    async with database() as session:
        await runs.process_run_messages(session)
        customer = await session.get(Customer, customer_ids[0])
        if change == "consent":
            session.add(ClientCommunicationPreference(customer_id=customer.id, marketing_consent=ConsentStatus.opted_out))
        else:
            await add_booking(session, customer,
                              datetime.now(KYIV)+timedelta(days=1) if change == "upcoming" else datetime.now(KYIV),
                              BookingStatus.confirmed if change == "upcoming" else BookingStatus.completed)
        await session.commit()
    clock.advance(1)
    await queue.process_one()
    assert sms_transport.requests == []
    async with database() as session:
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id))).one()
        assert recipient.status == MessageDeliveryStatus.skipped
        assert recipient.last_error


@pytest.mark.anyio
async def test_account_rate_and_priority_are_shared_by_independent_workers(database, sms_transport):
    from app.services.sms_queue import SmsQueueService
    producer = SmsQueueService(session_factory=database)
    sms_transport.latency = 0.2
    for number in range(10):
        await producer.enqueue("send", sms_payload(number), priority=10, idempotency_key=f"campaign-{number}")
    await producer.enqueue("send", sms_payload(11, "Sandbox OTP"), priority=0, idempotency_key="otp-first")
    await producer.enqueue("send", sms_payload(12, "Sandbox reminder"), priority=5, idempotency_key="service-second")
    await producer.enqueue("status", {"id_sms": ["existing-sandbox"]}, priority=20, idempotency_key="status-last")
    workers = [SmsQueueService(session_factory=database) for _ in range(3)]

    async def drain(worker):
        deadline = time.monotonic() + 10
        while len(sms_transport.requests) < 13 and time.monotonic() < deadline:
            await worker.process_one()
            await asyncio.sleep(0.01)

    await asyncio.gather(*(drain(worker) for worker in workers))
    assert len(sms_transport.requests) == 13
    assert sms_transport.requests[0]["payload"]["message"] == "Sandbox OTP"
    assert sms_transport.requests[1]["payload"]["message"] == "Sandbox reminder"
    starts = [record["at"] for record in sms_transport.requests]
    assert max(sum(start <= candidate < start+1 for candidate in starts) for start in starts) <= 8
    assert sms_transport.max_in_flight <= settings.sms_queue_concurrency


@pytest.mark.anyio
async def test_delayed_eligibility_cannot_bunch_actual_http_emissions(database, sms_transport):
    from app.services.sms_queue import SmsDispatchDecision, SmsQueueService
    arrived = 0
    first_two_ready = asyncio.Event()

    async def delayed_eligibility(_job):
        nonlocal arrived
        arrived += 1
        if arrived <= 2:
            if arrived == 2:
                first_two_ready.set()
            await asyncio.wait_for(first_two_ready.wait(), timeout=3)
        return SmsDispatchDecision()

    producer = SmsQueueService(session_factory=database)
    for number in range(16):
        await producer.enqueue("send", sms_payload(number), idempotency_key=f"delayed-{number}")
    workers = [SmsQueueService(session_factory=database, before_dispatch=delayed_eligibility) for _ in range(4)]

    async def drain(worker):
        deadline = time.monotonic()+10
        while len(sms_transport.requests) < 16 and time.monotonic() < deadline:
            await worker.process_one()
            await asyncio.sleep(0.005)

    await asyncio.gather(*(drain(worker) for worker in workers))
    assert len(sms_transport.requests) == 16
    starts = [record["at"] for record in sms_transport.requests]
    assert max(sum(start <= candidate < start+1 for candidate in starts) for start in starts) <= 8


@pytest.mark.anyio
async def test_concurrent_enqueue_idempotency_does_not_duplicate_provider_dispatch(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService
    workers = [SmsQueueService(session_factory=database) for _ in range(3)]
    results = await asyncio.gather(*(worker.enqueue("send", sms_payload(1), idempotency_key="same-send") for worker in workers))
    assert len({job.id for job in results}) == 1
    await asyncio.gather(*(worker.process_one() for worker in workers))
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(SmsQueueJob)) == 1
    assert len(sms_transport.requests) == 1


@pytest.mark.anyio
async def test_429_retry_after_defers_account_and_preserves_job_identity(database, sms_transport):
    from fastapi import HTTPException
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService, SmsTransportError
    clock = SimulatedClock()
    queue = SmsQueueService(session_factory=database, clock=clock)

    def rate_limited_once(ordinal, _url, _payload):
        if ordinal == 1:
            raise SmsTransportError(429, "Sandbox rate limit", code="rate_limited", retryable=True, retry_after_seconds=2)

    sms_transport.outcome = rate_limited_once
    job = await queue.enqueue("send", sms_payload(1), idempotency_key="429-retry")
    await queue.process_one()
    clock.advance(1)
    await queue.process_one()
    assert len(sms_transport.requests) == 1
    clock.advance(2)
    await queue.process_one()
    async with database() as session:
        recovered = await session.get(SmsQueueJob, job.id)
        assert recovered.attempts == 2
        assert recovered.provider_message_id is not None
    assert len(sms_transport.requests) == 2


@pytest.mark.anyio
async def test_last_retry_429_still_cools_down_other_campaigns_and_otp(database, sms_transport, monkeypatch):
    from app.services.sms_queue import SmsQueueService, SmsTransportError
    monkeypatch.setattr(settings, "sms_queue_max_attempts", 1)
    clock = SimulatedClock()
    queue = SmsQueueService(session_factory=database, clock=clock)

    def rate_limited_once(ordinal, _url, _payload):
        if ordinal == 1:
            raise SmsTransportError(429, "Sandbox quota", code="rate_limited", retryable=True, retry_after_seconds=3)

    sms_transport.outcome = rate_limited_once
    await queue.enqueue("send", sms_payload(1), idempotency_key="exhausted-429")
    await queue.process_one()
    await queue.enqueue("send", sms_payload(2, "Sandbox urgent OTP"), priority=0, idempotency_key="otp-after-429")
    clock.advance(1)
    await queue.process_one()
    assert len(sms_transport.requests) == 1
    clock.advance(3)
    await queue.process_one()
    assert len(sms_transport.requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("error_kind", ["timeout", "definite_rejection"])
async def test_send_failures_do_not_trigger_unproven_duplicate_retries(database, sms_transport, error_kind):
    from fastapi import HTTPException
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService, SmsTransportError
    clock = SimulatedClock()
    queue = SmsQueueService(session_factory=database, clock=clock)

    def fail(_ordinal, _url, _payload):
        if error_kind == "timeout":
            raise TimeoutError("Sandbox ambiguous acceptance")
        raise SmsTransportError(400, "Sandbox invalid recipient", code="recipient_rejected")

    sms_transport.outcome = fail
    job = await queue.enqueue("send", sms_payload(1), idempotency_key=error_kind)
    await queue.process_one()
    clock.advance(3600)
    await queue.process_one()
    async with database() as session:
        failed = await session.get(SmsQueueJob, job.id)
        assert failed.status == ("uncertain" if error_kind == "timeout" else "failed")
        assert failed.attempts == 1
        assert failed.error_code
    assert len(sms_transport.requests) == 1


@pytest.mark.anyio
async def test_restarting_worker_recovers_queued_job_but_not_uncertain_send(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService
    clock = SimulatedClock()
    producer = SmsQueueService(session_factory=database, clock=clock)
    queued = await producer.enqueue("send", sms_payload(1), idempotency_key="durable-queued")
    interrupted = await producer.enqueue("send", sms_payload(2), idempotency_key="durable-interrupted")
    async with database() as session:
        reserved = await session.get(SmsQueueJob, interrupted.id)
        reserved.status = "dispatching"
        reserved.attempts = 1
        reserved.claimed_at = clock.value-timedelta(minutes=1)
        reserved.transport_started_at = clock.value-timedelta(minutes=1)
        reserved.lease_expires_at = clock.value-timedelta(seconds=1)
        reserved.lease_token = "interrupted-worker-token"
        await session.commit()
    restarted = SmsQueueService(session_factory=database, clock=clock)
    await restarted.process_one()
    async with database() as session:
        sent = await session.get(SmsQueueJob, queued.id)
        uncertain = await session.get(SmsQueueJob, interrupted.id)
        assert sent.status == "accepted"
        assert uncertain.status == "uncertain"
        assert uncertain.error_code == "worker_interrupted"
        assert sent.payload == uncertain.payload == {}
    assert [request["payload"]["phone"] for request in sms_transport.requests] == [sms_payload(1)["phone"]]


@pytest.mark.anyio
async def test_read_only_status_timeout_retries_without_resending_sms(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService
    clock = SimulatedClock()
    queue = SmsQueueService(session_factory=database, clock=clock)

    def timeout_once(ordinal, _url, _payload):
        if ordinal == 1:
            raise TimeoutError("Sandbox status lookup timeout")

    sms_transport.outcome = timeout_once
    job = await queue.enqueue("status", {"id_sms": ["already-accepted-1"]}, idempotency_key="status-retry")
    await queue.process_one()
    clock.advance(5)
    await queue.process_one()
    async with database() as session:
        completed = await session.get(SmsQueueJob, job.id)
        assert completed.status == "accepted"
        assert completed.attempts == 2
    assert len(sms_transport.requests) == 2
    assert all(request["url"].endswith("/sms/status") for request in sms_transport.requests)


@pytest.mark.anyio
async def test_real_sms_service_otp_and_status_paths_share_durable_account_gate(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService
    first_queue = SmsQueueService(session_factory=database)
    second_queue = SmsQueueService(session_factory=database)
    for number in range(4):
        await first_queue.enqueue("send", sms_payload(number), priority=100, idempotency_key=f"marketing-{number}")
    await SmsService(queue=first_queue).send_otp_code("+380501111111", "123456")
    statuses = await SmsService(queue=second_queue).get_message_statuses(["previously-accepted"])
    assert sms_transport.requests[0]["payload"]["lifetime"] == settings.otp_code_ttl_minutes
    assert "123456" in sms_transport.requests[0]["payload"]["message"]
    assert "previously-accepted" in statuses
    async with database() as session:
        jobs = list(await session.scalars(select(SmsQueueJob)))
        assert len(jobs) == 6
        assert all(job.status == "accepted" and job.payload == {} for job in jobs)
        assert sorted(job.priority for job in jobs) == [0, 100, 100, 100, 100, 200]
        assert "123456" not in repr([job.context_json for job in jobs])


@pytest.mark.anyio
async def test_two_campaigns_and_otp_share_global_limit_with_cross_campaign_dedup(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.campaign_dispatch import CampaignDispatchService
    from app.services.campaign_runs import CampaignRunService
    from app.services.messaging import MessagingService, SmsMessageProvider
    from app.services.sms_queue import SmsQueueService, TERMINAL_STATES
    await seed_campaign_run(database, count=6, suffix=1)
    await seed_campaign_run(database, count=6, suffix=2)
    dispatch = CampaignDispatchService(session_factory=database)
    queue = SmsQueueService(session_factory=database, before_dispatch=dispatch.sms_job_eligibility,
                            after_outcome=dispatch.sms_job_outcome)
    runs = CampaignRunService(MessagingService(providers={MessageChannel.sms: SmsMessageProvider(SmsService(queue=queue))}))
    async with database() as session:
        assert await runs.process_run_messages(session, limit=100) == 18
    await SmsService(queue=queue).send_otp_code("+380501111111", "654321")
    assert "654321" in sms_transport.requests[0]["payload"]["message"]
    workers = [SmsQueueService(session_factory=database, before_dispatch=dispatch.sms_job_eligibility,
                              after_outcome=dispatch.sms_job_outcome) for _ in range(3)]

    async def drain(worker):
        for _ in range(500):
            await worker.process_one()
            async with database() as session:
                unfinished = await session.scalar(select(func.count()).select_from(SmsQueueJob)
                                                   .where(SmsQueueJob.status.not_in(TERMINAL_STATES)))
            if not unfinished:
                return
            await asyncio.sleep(0.01)

    await asyncio.gather(*(drain(worker) for worker in workers))
    async with database() as session:
        recipients = list(await session.scalars(select(MessageRecipient)))
        assert sum(recipient.status == MessageDeliveryStatus.sent for recipient in recipients) == 12
        assert sum(recipient.status == MessageDeliveryStatus.skipped for recipient in recipients) == 6
    assert len(sms_transport.requests) == 13
    starts = [record["at"] for record in sms_transport.requests]
    assert max(sum(start <= candidate < start+1 for candidate in starts) for start in starts) <= 8


@pytest.mark.anyio
async def test_thousand_personalized_campaign_recipients_have_independent_durable_outcomes(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.campaign_dispatch import CampaignDispatchService
    from app.services.campaign_runs import CampaignRunService
    from app.services.messaging import MessagingService, SmsMessageProvider
    from app.services.sms_queue import SmsQueueService, SmsTransportError

    campaign_id, run_id, _ = await seed_campaign_run(database, count=1000, sms_recipients_per_minute=480)
    clock = SimulatedClock()
    dispatch = CampaignDispatchService(session_factory=database, clock=lambda: clock.value)
    queue = SmsQueueService(session_factory=database, clock=clock,
                           before_dispatch=dispatch.sms_job_eligibility, after_outcome=dispatch.sms_job_outcome)
    messaging = MessagingService(providers={MessageChannel.sms: SmsMessageProvider(SmsService(queue=queue))})
    runs = CampaignRunService(messaging)

    def partial_failure(ordinal, _url, _payload):
        if ordinal % 100 == 0:
            raise SmsTransportError(400, "Sandbox invalid phone", code="recipient_rejected")
        if ordinal % 111 == 0:
            raise TimeoutError("Sandbox ambiguous provider timeout")

    sms_transport.outcome = partial_failure
    async with database() as session:
        assert await runs.process_run_messages(session, limit=1000) == 1000
        assert sms_transport.requests == []
        recipients = list(await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id)))
        assert len({row.sms_queue_job_id for row in recipients}) == 1000
    workers = [SmsQueueService(session_factory=database, clock=clock,
               before_dispatch=dispatch.sms_job_eligibility, after_outcome=dispatch.sms_job_outcome) for _ in range(3)]

    async def drain(worker):
        for _ in range(3000):
            if len(sms_transport.requests) >= 1000:
                return
            clock.advance(0.2)
            await worker.process_one()

    await asyncio.gather(*(drain(worker) for worker in workers))
    assert len(sms_transport.requests) == 1000
    assert len({record["payload"]["message"] for record in sms_transport.requests}) == 1000
    async with database() as session:
        jobs = list(await session.scalars(select(SmsQueueJob)))
        assert len(jobs) == 1000
        assert sum(job.status == "accepted" for job in jobs) == 981
        assert sum(job.status == "failed" for job in jobs) == 10
        assert sum(job.status == "uncertain" for job in jobs) == 9
        assert len({job.provider_message_id for job in jobs if job.status == "accepted"}) == 981
        assert all(job.payload == {} for job in jobs)
        run = await session.get(CampaignRun, run_id)
        campaign = await session.get(Campaign, campaign_id)
        assert run.status == "completed"
        progress = await dispatch.progress(session, campaign, run=run)
        assert progress["total"] == 1000
        assert progress["counts"]["accepted"] == 981
        assert progress["counts"]["failed"] == 10
        assert progress["counts"]["uncertain"] == 9


@pytest.mark.anyio
async def test_delivery_receipts_update_queue_and_recipient_monotonically(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    _, run_id, _ = await seed_campaign_run(database)
    clock = SimulatedClock()
    runs, queue, _ = campaign_worker(database, clock)
    async with database() as session:
        await runs.process_run_messages(session)
    await queue.process_one()
    async with database() as session:
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id))).one()
        job_id, provider_id = recipient.sms_queue_job_id, recipient.provider_message_id
        accepted = (await session.get(SmsQueueJob, job_id)).accepted_at
    for receipt in ("DELIVRD", "UNDELIV", "ENROUTE"):
        sms_transport.outcome = lambda _ordinal, url, payload: (
            {"success_request": {"info": {str(item): receipt for item in payload["id_sms"]}}}
            if url.endswith("/sms/status") else None)
        clock.advance(1)
        await queue.enqueue("status", {"id_sms": [provider_id]}, idempotency_key=f"receipt-{receipt}")
        await queue.process_one()
        async with database() as session:
            job = await session.get(SmsQueueJob, job_id)
            recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id))).one()
            assert job.status == "delivered"
            assert job.accepted_at == accepted and job.delivered_at is not None
            assert recipient.status == MessageDeliveryStatus.delivered
    assert len([record for record in sms_transport.requests if record["url"].endswith("/sms/send")]) == 1


@pytest.mark.anyio
async def test_sms_queue_migration_round_trip_preserves_segment_run_history(database):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    _, run_id, _ = await seed_campaign_run(database)
    async with database() as session:
        for sql in ("ALTER TABLE message_recipients DROP COLUMN sms_queue_job_id CASCADE",
                    "DROP TABLE sms_queue_jobs", "DROP TABLE sms_account_throttles"):
            await session.execute(text(sql))
        spec = importlib.util.spec_from_file_location("sms_queue_migration", Path(__file__).parents[1] / "alembic/versions/0069_sms_queue_throttling.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        def run_migration(connection, direction):
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, direction)()

        connection = await session.connection()
        for direction in ("upgrade", "downgrade", "upgrade"):
            await connection.run_sync(run_migration, direction)
            assert await session.scalar(text("SELECT count(*) FROM message_recipients WHERE run_id=:id"), {"id": run_id}) == 1
            assert await session.scalar(text("SELECT audience_count FROM campaign_runs WHERE id=:id"), {"id": run_id}) == 1
        await session.commit()


@pytest.mark.anyio
async def test_activity_retry_preserves_one_secure_token_and_atomic_queue_payload(database, sms_transport, monkeypatch):
    from app.models.booking import BookingStatus
    from app.models.customer_activity import CustomerActivityAccessToken
    from app.models.messaging import CampaignStatus, CampaignType, MessagePurpose
    from app.models.sms_queue import SmsQueueJob
    from app.services import customer_activity_notifications
    clock = SimulatedClock()
    _, queue, _ = campaign_worker(database, clock)
    monkeypatch.setattr(customer_activity_notifications, "AsyncSessionLocal", database)
    notification = customer_activity_notifications.CustomerActivityNotificationService(SmsService(queue=queue))
    async with database() as session:
        customer = await add_customer(session)
        booking = await add_booking(session, customer, datetime.now(KYIV)+timedelta(days=1), BookingStatus.confirmed)
        campaign = await add_campaign(session, [])
        campaign.status = CampaignStatus.active
        campaign.type = CampaignType.booking_confirmation
        campaign.purpose = MessagePurpose.transactional
        recipient = MessageRecipient(campaign_id=campaign.id, customer_id=customer.id, appointment_id=booking.id,
                                     channel=MessageChannel.sms, idempotency_key="customer-activity:sandbox",
                                     rendered_message="Manage: {manage_url}. Cancel: {cancel_url}")
        session.add(recipient)
        await session.commit()
        recipient_id = recipient.id
    assert await notification._dispatch(recipient_id) is False
    async with database() as session:
        token = (await session.scalars(select(CustomerActivityAccessToken))).one()
        token_id = token.id
        queued = (await session.scalars(select(SmsQueueJob))).one()
        original_body = queued.payload["message"]
        assert "{manage_url}" not in original_body and "http" in original_body
    assert await notification._dispatch(recipient_id) is False
    async with database() as session:
        tokens = list(await session.scalars(select(CustomerActivityAccessToken)))
        assert len(tokens) == 1 and tokens[0].id == token_id and tokens[0].revoked_at is None
        jobs = list(await session.scalars(select(SmsQueueJob)))
        assert len(jobs) == 1 and jobs[0].payload["message"] == original_body
    await queue.process_one()
    assert len(sms_transport.requests) == 1
    assert sms_transport.requests[0]["payload"]["message"] == original_body
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        assert recipient.status == MessageDeliveryStatus.sent
        assert recipient.rendered_message != original_body
        assert (await session.get(CustomerActivityAccessToken, token_id)).revoked_at is None


@pytest.mark.anyio
async def test_atomic_outbox_rollback_leaves_no_visible_sms_job(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService
    queue = SmsQueueService(session_factory=database)
    async with database() as session:
        await queue.enqueue("send", sms_payload(1), idempotency_key="rollback-outbox", external_session=session)
        async with database() as reader:
            assert await reader.scalar(select(func.count()).select_from(SmsQueueJob)) == 0
        await session.rollback()
    async with database() as reader:
        assert await reader.scalar(select(func.count()).select_from(SmsQueueJob)) == 0
    assert sms_transport.requests == []


@pytest.mark.anyio
async def test_failed_projection_must_recover_before_retry_can_dispatch(database, sms_transport):
    from app.services.sms_queue import SmsQueueService, SmsTransportError
    clock = SimulatedClock()
    unavailable = True

    async def project(_job):
        if unavailable:
            raise RuntimeError("Sandbox projection database unavailable")

    def first_429(ordinal, _url, _payload):
        if ordinal == 1:
            raise SmsTransportError(429, "Sandbox rate limited", code="rate_limited", retryable=True)

    sms_transport.outcome = first_429
    queue = SmsQueueService(session_factory=database, clock=clock, after_outcome=project)
    await queue.enqueue("send", sms_payload(1), idempotency_key="projection-retry")
    await queue.process_one()
    clock.advance(5)
    await queue.process_one()
    assert len(sms_transport.requests) == 1
    unavailable = False
    await queue.process_one()  # Reconcile the durable outcome first.
    assert len(sms_transport.requests) == 1
    await queue.process_one()
    assert len(sms_transport.requests) == 2


@pytest.mark.anyio
async def test_cancel_between_eligibility_and_transport_prevents_http_dispatch(database, sms_transport):
    _, run_id, _ = await seed_campaign_run(database)
    clock = SimulatedClock()
    runs, queue, dispatch = campaign_worker(database, clock)
    ready = asyncio.Event()
    continue_dispatch = asyncio.Event()

    async def pause_after_eligibility(job):
        result = await dispatch.sms_job_eligibility(job)
        ready.set()
        await continue_dispatch.wait()
        return result

    queue.before_dispatch = pause_after_eligibility
    async with database() as session:
        await runs.process_run_messages(session)
    task = asyncio.create_task(queue.process_one())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        async with database() as session:
            run = await session.get(CampaignRun, run_id)
            await runs.cancel_run_unsent(session, run)
        continue_dispatch.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert sms_transport.requests == []
    async with database() as session:
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id))).one()
        assert recipient.status == MessageDeliveryStatus.skipped


@pytest.mark.anyio
@pytest.mark.parametrize("receipt", ["DELIVRD", "UNDELIV"])
async def test_waitlist_offer_outbox_and_receipts_preserve_business_state(database, sms_transport, monkeypatch, receipt):
    from app.models.booking import Master
    from app.models.messaging import CampaignStatus, CampaignType, MessagePurpose
    from app.models.sms_queue import SmsQueueJob
    from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
    from app.services import sms_notification_queue
    from app.services.sms_queue import SmsQueueService
    from app.services.waitlist_offers import WaitlistOfferService, WAITLIST_OFFER_SMS_LOCATION_KEY
    monkeypatch.setattr(sms_notification_queue, "AsyncSessionLocal", database)
    clock = SimulatedClock()
    queue = SmsQueueService(session_factory=database, clock=clock)
    service = WaitlistOfferService(sms_service=SmsService(queue=queue))
    async with database() as session:
        customer = await add_customer(session)
        master = Master(full_name="Sandbox public master")
        session.add(master)
        await session.flush()
        request = WaitlistRequest(customer_id=customer.id, preferred_master_id=master.id,
                                  cancel_token_hash="a" * 64, dedup_key_hash="b" * 64,
                                  desired_date=(clock.value + timedelta(days=1)).date(), duration_minutes=30,
                                  notification_consent=True, expires_at=clock.value + timedelta(days=1))
        session.add(request)
        await session.flush()
        offer = WaitlistOffer(request_id=request.id, master_id=master.id,
                             start_at=clock.value + timedelta(days=1), end_at=clock.value + timedelta(days=1, minutes=30),
                             token_hash="c" * 64, expires_at=clock.value + timedelta(minutes=15), scheduled_at=clock.value)
        session.add_all([offer, Campaign(name="Waitlist sandbox", type=CampaignType.booking_confirmation,
                                        purpose=MessagePurpose.transactional, status=CampaignStatus.active,
                                        channel=MessageChannel.sms, location_key=WAITLIST_OFFER_SMS_LOCATION_KEY)])
        await session.flush()
        assert await service._send_offer(session, offer, "sandbox-secret-offer-capability") is False
        offer_id, request_id = offer.id, request.id
    assert sms_transport.requests == []
    async with database() as session:
        job = (await session.scalars(select(SmsQueueJob))).one()
        job_id = job.id
        assert "sandbox-secret-offer-capability" in job.payload["message"]
        assert "sandbox-secret-offer-capability" not in str(job.context_json)
    await queue.process_one()
    async with database() as session:
        job = await session.get(SmsQueueJob, job_id)
        provider_id = job.provider_message_id
        assert job.status == "accepted" and job.payload == {}
        assert (await session.get(WaitlistOffer, offer_id)).status == WaitlistOfferStatus.sent
        recipient = (await session.scalars(select(MessageRecipient))).one()
        assert recipient.sms_queue_job_id == job_id and recipient.status == MessageDeliveryStatus.sent
        assert "sandbox-secret-offer-capability" not in recipient.rendered_message
    sms_transport.outcome = lambda _ordinal, url, payload: (
        {"success_request": {"info": {str(item): receipt for item in payload["id_sms"]}}}
        if url.endswith("/sms/status") else None)
    clock.advance(1)
    await queue.enqueue("status", {"id_sms": [provider_id]}, idempotency_key="waitlist-receipt")
    await queue.process_one()
    async with database() as session:
        offer = await session.get(WaitlistOffer, offer_id)
        request = await session.get(WaitlistRequest, request_id)
        recipient = (await session.scalars(select(MessageRecipient))).one()
        if receipt == "DELIVRD":
            assert offer.status == WaitlistOfferStatus.delivered and offer.delivered_at is not None
            assert recipient.status == MessageDeliveryStatus.delivered
            assert request.status == WaitlistStatus.offered
        else:
            assert offer.status == WaitlistOfferStatus.cancelled
            assert recipient.status == MessageDeliveryStatus.failed
            assert request.status == WaitlistStatus.active
    if receipt == "DELIVRD":
        stale = await queue.get(job_id)
        stale.status = "accepted"
        await sms_notification_queue.sms_job_outcome(stale)
        async with database() as session:
            assert (await session.get(WaitlistOffer, offer_id)).status == WaitlistOfferStatus.delivered


@pytest.mark.anyio
async def test_concurrent_outcome_projection_invokes_callback_once(database, sms_transport):
    from app.models.sms_queue import SmsQueueJob
    from app.services.sms_queue import SmsQueueService
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def project(_job):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    queue = SmsQueueService(session_factory=database, after_outcome=project)
    job = await queue.enqueue("send", sms_payload(1), idempotency_key="serialize-projection")
    async with database() as session:
        row = await session.get(SmsQueueJob, job.id)
        row.error_code = "rate_limited"
        row.available_at = datetime.now(UTC) + timedelta(minutes=1)
        await session.commit()
    first = asyncio.create_task(queue._project(job.id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await queue._project(job.id) is False
        assert calls == 1
        release.set()
        assert await asyncio.wait_for(first, timeout=5) is True
        assert await queue._project(job.id) is False
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.anyio
async def test_waiting_high_priority_claim_cannot_be_overtaken_at_transport(database, sms_transport):
    from app.services.sms_queue import SmsQueueService, SmsDispatchDecision
    clock = SimulatedClock()
    urgent_claimed = asyncio.Event()
    release_urgent = asyncio.Event()

    async def eligibility(job):
        if job.priority == 0:
            urgent_claimed.set()
            await release_urgent.wait()
        return SmsDispatchDecision()

    producer = SmsQueueService(session_factory=database, clock=clock)
    await producer.enqueue('send', sms_payload(1, 'Urgent OTP'), priority=0, idempotency_key='urgent')
    await producer.enqueue('send', sms_payload(2, 'Marketing'), priority=10, idempotency_key='marketing')
    urgent = SmsQueueService(session_factory=database, clock=clock, before_dispatch=eligibility)
    marketing = SmsQueueService(session_factory=database, clock=clock, before_dispatch=eligibility)
    urgent_task = asyncio.create_task(urgent.process_one())
    try:
        await asyncio.wait_for(urgent_claimed.wait(), timeout=3)
        await marketing.process_one()
        assert sms_transport.requests == [], 'A lower-priority claim overtook an eligible urgent claim before transport'
    finally:
        release_urgent.set()
        await urgent_task
    assert sms_transport.requests[0]['payload']['message'] == 'Urgent OTP'
    clock.advance(2)
    await marketing.process_one()
    assert [item['payload']['message'] for item in sms_transport.requests] == ['Urgent OTP', 'Marketing']
