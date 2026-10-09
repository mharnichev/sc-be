"""Recovery fairness with real PostgreSQL and synthetic outcome callbacks only."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.models.sms_queue import SmsQueueJob
from app.services.sms_queue import SmsDispatchDecision, SmsQueueService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_sms_queue_integration import SimulatedClock, sms_payload


pytestmark = pytest.mark.anyio


class ProjectionPair:
    def __init__(self, database):
        self.database = database
        self.clock = SimulatedClock()
        self.ids = {}
        self.calls = []
        self.successes = []
        self.callback_states = []
        self.faults = set()
        self.transport_calls = []
        self.dispatch_calls = []
        self.queue = SmsQueueService(
            session_factory=database, clock=self.clock, transport=self.transport,
            before_dispatch=self.dispatch, after_outcome=self.project,
        )
        self.original = None

    async def transport(self, job):
        self.transport_calls.append(job.id)
        raise AssertionError("Projection recovery must never call the provider")

    async def dispatch(self, job):
        self.dispatch_calls.append(job.id)
        raise AssertionError("Accepted jobs must never be claimed for dispatch")

    async def project(self, job):
        label = next((label for label, identifier in self.ids.items() if identifier == job.id), "unknown")
        self.calls.append(label)
        # Check these outside the callback: _project intentionally catches its
        # exceptions, so assertions here could masquerade as the injected fault.
        self.callback_states.append((label, job.status, job.attempts, job.payload,
                                     job.provider_message_id, job.lease_token))
        if label in self.faults:
            raise RuntimeError(f"Synthetic projection fault for {label}")
        self.successes.append(label)

    async def seed(self):
        for number, label in enumerate(("A", "B"), 1):
            job = await self.queue.enqueue(
                "send", sms_payload(number), idempotency_key=f"synthetic-projection-{label}",
                expires_at=self.clock.value + timedelta(hours=1),
            )
            self.ids[label] = job.id
        async with self.database() as session:
            for number, label in enumerate(("A", "B"), 1):
                job = await session.get(SmsQueueJob, self.ids[label])
                accepted_at = self.clock.value - timedelta(minutes=3 - number)
                job.status = "accepted"
                job.attempts = 1
                job.payload = {}
                job.provider_message_id = f"synthetic-accepted-{label}"
                job.result_json = {"success_request": {"info": {job.provider_message_id: f"synthetic-{label}"}}}
                job.claimed_at = job.transport_started_at = job.accepted_at = accepted_at
                job.lease_token = job.lease_expires_at = None
                job.outcome_projected_at = None
                # Explicit distinct times make the oldest candidate unambiguous.
                job.created_at = job.updated_at = accepted_at
            await session.commit()
        self.original = await self.durable_state()
        assert self.original["A"]["accepted_at"] < self.original["B"]["accepted_at"]
        assert await self.projected() == {"A": False, "B": False}

    async def durable_state(self):
        # Projection scheduling metadata may evolve; these delivery/idempotency
        # fields must remain unchanged throughout recovery and replay.
        fields = (
            "id", "account_key", "idempotency_key", "operation", "priority", "payload",
            "context_json", "status", "attempts", "available_at", "claimed_at",
            "transport_started_at", "lease_expires_at", "lease_token", "expires_at",
            "result_json", "provider_message_id", "accepted_at", "delivered_at",
            "delivery_status", "error_code", "error_detail", "created_at",
        )
        async with self.database() as session:
            rows = list(await session.scalars(select(SmsQueueJob)))
            assert len(rows) == 2 and {row.id for row in rows} == set(self.ids.values())
            return {label: {field: getattr(next(row for row in rows if row.id == identifier), field)
                            for field in fields} for label, identifier in self.ids.items()}

    async def projected(self):
        async with self.database() as session:
            rows = list(await session.scalars(select(SmsQueueJob)))
            return {label: next(row for row in rows if row.id == identifier).outcome_projected_at is not None
                    for label, identifier in self.ids.items()}

    async def tick(self):
        result = await asyncio.wait_for(self.queue.process_one(), timeout=10)
        self.clock.advance(settings.sms_queue_poll_seconds)
        return result

    async def assert_delivery_unchanged(self):
        assert self.transport_calls == self.dispatch_calls == []
        assert await self.durable_state() == self.original
        for label, status, attempts, payload, provider_id, lease_token in self.callback_states:
            assert label in {"A", "B"}
            assert (status, attempts, payload, provider_id, lease_token) == (
                "accepted", 1, {}, f"synthetic-accepted-{label}", None,
            )


@pytest.fixture
async def projection_pair(database):
    pair = ProjectionPair(database)
    await pair.seed()
    return pair


async def test_failing_oldest_projection_does_not_starve_healthy_recovery(projection_pair, record_property):
    pair = projection_pair
    pair.faults.add("A")
    results = [await pair.tick() for _ in range(5)]
    after_five = await pair.projected()
    calls_after_five = list(pair.calls)
    evidence = {
        "process_one_results": results, "callback_calls": calls_after_five,
        "callback_counts": dict(Counter(calls_after_five)), "projected_after_five": after_five,
        "provider_calls": list(pair.transport_calls),
    }
    record_property("projection_recovery_fairness", json.dumps(evidence, sort_keys=True))
    assert calls_after_five and calls_after_five[0] == "A"
    assert not after_five["A"] and "A" not in pair.successes
    assert pair.successes == (["B"] if after_five["B"] else [])
    await pair.assert_delivery_unchanged()

    # Run controls before the regression assertion, even on the broken source.
    # B's callback and persistence work when selected directly.
    assert await pair.queue._project(pair.ids["B"]) is (not after_five["B"])
    assert await pair.projected() == {"A": False, "B": True}
    assert pair.calls.count("B") == pair.successes.count("B") == 1
    pair.faults.clear()
    pair.clock.advance(settings.sms_queue_retry_max_seconds + 1)
    assert await pair.tick() is True
    assert await pair.projected() == {"A": True, "B": True}
    assert Counter(pair.successes) == {"A": 1, "B": 1}
    calls_before_idle = list(pair.calls)
    assert await pair.tick() is False
    assert pair.calls == calls_before_idle
    await pair.assert_delivery_unchanged()

    assert after_five["B"], (
        "Healthy accepted job B must project within five recovery iterations despite failing oldest A",
        evidence,
    )


async def test_successful_recovery_keeps_fifo_order_and_projects_each_job_once(projection_pair):
    pair = projection_pair
    assert await pair.tick() is True
    assert pair.calls == pair.successes == ["A"]
    assert await pair.projected() == {"A": True, "B": False}
    assert await pair.tick() is True
    assert pair.calls == pair.successes == ["A", "B"]
    assert await pair.projected() == {"A": True, "B": True}
    assert await pair.tick() is False
    assert await pair.queue._project(pair.ids["A"]) is False
    assert await pair.queue._project(pair.ids["B"]) is False
    assert pair.calls == ["A", "B"]
    await pair.assert_delivery_unchanged()


async def test_direct_projection_fault_release_and_idempotent_replay_never_resend(projection_pair):
    pair = projection_pair
    pair.faults.add("A")
    assert await pair.queue._project(pair.ids["A"]) is False
    assert await pair.queue._project(pair.ids["B"]) is True
    assert await pair.projected() == {"A": False, "B": True}
    assert await pair.queue._project(pair.ids["B"]) is False
    pair.faults.clear()
    assert await pair.queue._project(pair.ids["A"]) is True
    assert await pair.projected() == {"A": True, "B": True}
    assert pair.calls == ["A", "B", "A"] and pair.successes == ["B", "A"]
    for number, label in enumerate(("A", "B"), 1):
        replay = await pair.queue.enqueue(
            "send", sms_payload(number), idempotency_key=f"synthetic-projection-{label}",
        )
        assert replay.id == pair.ids[label] and replay.status == "accepted"
    assert await pair.tick() is False
    assert pair.calls == ["A", "B", "A"]
    await pair.assert_delivery_unchanged()


async def stored_job(pair, label):
    async with pair.database() as session:
        return (await session.execute(select(SmsQueueJob.__table__).where(
            SmsQueueJob.id == pair.ids[label],
        ))).mappings().one()


async def test_projection_retry_survives_worker_restart_and_expires_at_exact_clock_boundary(projection_pair):
    pair = projection_pair
    pair.faults.add("A")
    before = dict(await stored_job(pair, "A"))
    failed_at = pair.clock.value
    assert await pair.queue._project(pair.ids["A"]) is False
    backed_off = dict(await stored_job(pair, "A"))
    retry_at = failed_at + timedelta(seconds=30)
    assert backed_off == {**before, "projection_retry_at": retry_at}
    await pair.assert_delivery_unchanged()

    pair.queue = SmsQueueService(
        session_factory=pair.database, clock=pair.clock, transport=pair.transport,
        before_dispatch=pair.dispatch, after_outcome=pair.project,
    )
    assert await pair.tick() is True
    assert pair.calls == ["A", "B"]
    assert await pair.projected() == {"A": False, "B": True}
    pair.clock.value = retry_at - timedelta(microseconds=1)
    assert await pair.tick() is False
    assert pair.calls == ["A", "B"]
    assert dict(await stored_job(pair, "A")) == backed_off
    pair.clock.value = retry_at
    assert await pair.tick() is False
    assert pair.calls == ["A", "B", "A"]
    retry_again = dict(await stored_job(pair, "A"))
    assert retry_again == {**before, "projection_retry_at": retry_at + timedelta(seconds=30)}
    pair.faults.clear()
    pair.clock.value = retry_again["projection_retry_at"]
    assert await pair.tick() is True
    assert await pair.projected() == {"A": True, "B": True}
    assert (await stored_job(pair, "A"))["projection_retry_at"] is None
    assert Counter(pair.successes) == {"A": 1, "B": 1}
    await pair.assert_delivery_unchanged()


async def test_projection_failure_uses_postgresql_clock_without_changing_public_update_time(projection_pair):
    pair = projection_pair
    pair.faults.add("A")
    before = dict(await stored_job(pair, "A"))
    queue = SmsQueueService(session_factory=pair.database, transport=pair.transport, after_outcome=pair.project)
    async with pair.database() as session:
        lower = await session.scalar(text("SELECT clock_timestamp()"))
    assert await queue._project(pair.ids["A"]) is False
    async with pair.database() as session:
        upper = await session.scalar(text("SELECT clock_timestamp()"))
    after = dict(await stored_job(pair, "A"))
    retry_at = after["projection_retry_at"]
    assert lower + timedelta(seconds=30) <= retry_at <= upper + timedelta(seconds=30)
    assert after == {**before, "projection_retry_at": retry_at}
    await pair.assert_delivery_unchanged()


async def test_committed_callback_acknowledgement_loss_replays_effect_once_after_backoff(projection_pair):
    pair = projection_pair
    async with pair.database() as session:
        await session.execute(text("CREATE TABLE synthetic_projection_effects (job_id varchar(36) PRIMARY KEY)"))
        await session.commit()
    callback_calls = []

    async def committed_callback(job):
        callback_calls.append(job.id)
        async with pair.database() as session:
            await session.execute(text("INSERT INTO synthetic_projection_effects (job_id) VALUES (:job_id) "
                                       "ON CONFLICT (job_id) DO NOTHING"), {"job_id": job.id})
            await session.commit()
        if job.id == pair.ids["A"] and callback_calls.count(job.id) == 1:
            raise OSError("Synthetic callback acknowledgement loss after actual commit")

    pair.queue.after_outcome = committed_callback
    before = dict(await stored_job(pair, "A"))
    assert await pair.tick() is False
    backed_off = dict(await stored_job(pair, "A"))
    assert backed_off["projection_retry_at"] is not None
    assert backed_off == {**before, "projection_retry_at": backed_off["projection_retry_at"]}
    async with pair.database() as session:
        assert list(await session.scalars(text("SELECT job_id FROM synthetic_projection_effects"))) == [pair.ids["A"]]
    assert await pair.tick() is True
    assert callback_calls == [pair.ids["A"], pair.ids["B"]]
    pair.clock.value = backed_off["projection_retry_at"]
    assert await pair.tick() is True
    assert callback_calls == [pair.ids["A"], pair.ids["B"], pair.ids["A"]]
    assert await pair.projected() == {"A": True, "B": True}
    assert (await stored_job(pair, "A"))["projection_retry_at"] is None
    async with pair.database() as session:
        assert set(await session.scalars(text("SELECT job_id FROM synthetic_projection_effects"))) == set(pair.ids.values())
    assert await pair.tick() is False
    await pair.assert_delivery_unchanged()


@pytest.mark.parametrize("changed", ["status", "attempts", "projected"])
async def test_failed_old_callback_cannot_delay_a_new_or_already_projected_outcome(projection_pair, changed):
    pair = projection_pair
    entered, release = asyncio.Event(), asyncio.Event()
    callback_calls = []

    async def delayed_failure(job):
        callback_calls.append(job.id)
        entered.set()
        await release.wait()
        raise OSError("Synthetic retired callback failure")

    pair.queue.after_outcome = delayed_failure
    task = asyncio.create_task(pair.queue._project(pair.ids["A"]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with pair.database() as session:
            # A callback must not retain a queue row lock while business work
            # runs. This independent writer must obtain NOWAIT immediately.
            row = await session.get(SmsQueueJob, pair.ids["A"], with_for_update={"nowait": True})
            if changed == "status":
                row.status, row.delivery_status = "delivered", "DELIVRD"
                row.delivered_at = pair.clock.value
            elif changed == "attempts":
                row.attempts += 1
            else:
                row.outcome_projected_at = pair.clock.value
            row.projection_retry_at = None
            row.updated_at = pair.clock.value
            await session.commit()
        new_outcome = dict(await stored_job(pair, "A"))
        release.set()
        assert await asyncio.wait_for(task, timeout=10) is False
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert callback_calls == [pair.ids["A"]]
    assert dict(await stored_job(pair, "A")) == new_outcome
    assert new_outcome["projection_retry_at"] is None
    assert pair.transport_calls == pair.dispatch_calls == []


@pytest.mark.parametrize("operation", ["send", "status"])
async def test_finish_clears_old_projection_backoff_before_immediate_projection(projection_pair, operation):
    pair = projection_pair
    target = "A" if operation == "send" else "B"
    async with pair.database() as session:
        row = await session.get(SmsQueueJob, pair.ids[target])
        row.status, row.operation = "dispatching", operation
        row.lease_token = "synthetic-finish-owner"
        row.lease_expires_at = pair.clock.value + timedelta(minutes=1)
        row.projection_retry_at = pair.clock.value + timedelta(seconds=30)
        if operation == "status":
            original = await session.get(SmsQueueJob, pair.ids["A"])
            original.projection_retry_at = pair.clock.value + timedelta(seconds=30)
        await session.commit()
        claimed = row
    observed = []

    async def projected(job):
        observed.append((job.id, job.status, job.projection_retry_at))

    pair.queue.after_outcome = projected
    info = {"synthetic-finished-send": "synthetic-phone"} if operation == "send" else {"synthetic-accepted-A": "DELIVRD"}
    await pair.queue._finish(claimed, result={"success_request": {"info": info}})
    expected = [(pair.ids[target], "accepted", None)]
    if operation == "status":
        expected.append((pair.ids["A"], "delivered", None))
    assert observed == expected
    for identifier, status, _ in expected:
        async with pair.database() as session:
            row = await session.get(SmsQueueJob, identifier)
            assert row.status == status and row.projection_retry_at is None and row.outcome_projected_at is not None
    assert pair.transport_calls == pair.dispatch_calls == []


@pytest.mark.parametrize("old_status", ["queued", "dispatching"])
async def test_recovered_outcome_clears_old_projection_backoff(projection_pair, old_status):
    pair = projection_pair
    async with pair.database() as session:
        row = await session.get(SmsQueueJob, pair.ids["A"])
        row.status = old_status
        row.projection_retry_at = pair.clock.value + timedelta(seconds=30)
        if old_status == "queued":
            row.expires_at = pair.clock.value - timedelta(seconds=1)
        else:
            row.lease_expires_at = pair.clock.value - timedelta(seconds=1)
            row.lease_token = "synthetic-interrupted-owner"
        await session.commit()
    async with pair.database() as session:
        recovered = await pair.queue._recover(session, pair.clock.value)
        assert recovered == [pair.ids["A"]]
        row = await session.get(SmsQueueJob, pair.ids["A"])
        assert row.status == ("cancelled" if old_status == "queued" else "uncertain")
        assert row.projection_retry_at is None and row.outcome_projected_at is None
        await session.commit()
    assert (await stored_job(pair, "A"))["projection_retry_at"] is None
    assert pair.calls == pair.transport_calls == pair.dispatch_calls == []


async def test_new_claim_resets_projection_backoff_before_dispatch_eligibility(projection_pair):
    pair = projection_pair
    async with pair.database() as session:
        row = await session.get(SmsQueueJob, pair.ids["A"])
        row.status = "queued"
        row.projection_retry_at = pair.clock.value + timedelta(seconds=30)
        row.outcome_projected_at = pair.clock.value
        await session.commit()
    observed = []

    async def stop_before_transport(job):
        observed.append((job.status, job.projection_retry_at, job.outcome_projected_at))
        return SmsDispatchDecision("skip", "synthetic-claim-control")

    pair.queue.before_dispatch = stop_before_transport
    pair.queue.after_outcome = None
    assert await pair.tick() is True
    assert observed == [("dispatching", None, None)]
    row = await stored_job(pair, "A")
    assert row["status"] == "skipped" and row["projection_retry_at"] is None
    assert pair.transport_calls == []


async def test_campaign_cancellation_clears_projection_backoff_for_its_new_outcome(projection_pair):
    from app.models.campaign_run import CampaignRun
    from app.models.messaging import MessageDeliveryStatus, MessageRecipient
    from app.services.campaign_runs import CampaignRunService
    from tests.test_sms_queue_integration import seed_campaign_run

    pair = projection_pair
    _, run_id, _ = await seed_campaign_run(pair.database)
    async with pair.database() as session:
        row = await session.get(SmsQueueJob, pair.ids["A"])
        row.status = "queued"
        row.projection_retry_at = pair.clock.value + timedelta(seconds=30)
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id))).one()
        recipient.sms_queue_job_id = row.id
        await session.commit()
        run = await session.get(CampaignRun, run_id)
        assert await CampaignRunService().cancel_run_unsent(session, run) == 1
    async with pair.database() as session:
        row = await session.get(SmsQueueJob, pair.ids["A"])
        assert row.status == "cancelled" and row.error_code == "campaign_run_cancelled"
        assert row.projection_retry_at is None and row.outcome_projected_at is None
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run_id))).one()
        assert recipient.status == MessageDeliveryStatus.skipped and recipient.last_error == "campaign_run_cancelled"
        assert (await session.get(CampaignRun, run_id)).status == "cancelled"
    assert pair.calls == pair.transport_calls == pair.dispatch_calls == []
