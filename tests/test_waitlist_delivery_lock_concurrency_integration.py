"""Real PostgreSQL waitlist delivery/cancel lock ordering, without provider I/O."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.messaging import MessageLog, MessageRecipient
from app.models.sms_queue import SmsQueueJob
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.services import sms_notification_queue
from app.services.sms import SmsService
from app.services.sms_queue import SmsQueueService
from app.services.waitlist import WaitlistService
from app.services.waitlist_offers import WaitlistOfferService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_sms_business_final_admission_integration import seed_waitlist, waitlist_queue
from tests.test_sms_queue_integration import SimulatedClock, sms_transport  # noqa: F401


pytestmark = pytest.mark.anyio
CANCEL_TOKEN = "synthetic-delivery-cancel-token"


async def bounded(operation, *, timeout=10):
    return await asyncio.wait_for(operation, timeout=timeout)


def locks_entity(statement, entity):
    return getattr(statement, "_for_update_arg", None) is not None and any(
        description.get("entity") is entity for description in getattr(statement, "column_descriptions", ())
    )


class DeliveryCancelRace:
    def __init__(self, database, monkeypatch):
        self.database = database
        self.monkeypatch = monkeypatch
        self.pids = {}
        self.edges = []
        self.cancel_holds_request = asyncio.Event()
        self.delivery_started = asyncio.Event()
        self.delivery_holds_offer = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self.release_delivery = asyncio.Event()

    async def prepare(self, session, label):
        await bounded(session.execute(text("SET LOCAL lock_timeout = '10s'")))
        await bounded(session.execute(text("SET LOCAL statement_timeout = '15s'")))
        self.pids[label] = await bounded(session.scalar(text("SELECT pg_backend_pid()")))
        assert len(set(self.pids.values())) == len(self.pids)
        execute, get = session.execute, session.get

        async def after_lock(entity):
            if label == "cancel" and entity is WaitlistRequest and not self.cancel_holds_request.is_set():
                self.cancel_holds_request.set()
                await bounded(self.release_cancel.wait())
            if label == "delivery" and entity is WaitlistOffer and not self.delivery_holds_offer.is_set():
                self.delivery_holds_offer.set()
                await bounded(self.release_delivery.wait())

        async def execute_with_gate(statement, *args, **kwargs):
            result = await execute(statement, *args, **kwargs)
            entity = next((candidate for candidate in (WaitlistRequest, WaitlistOffer)
                           if locks_entity(statement, candidate)), None)
            if entity is not None:
                frozen = result.freeze()
                if frozen().scalars().all():
                    await after_lock(entity)
                return frozen()
            return result

        async def get_with_gate(entity, identity, *args, **kwargs):
            result = await get(entity, identity, *args, **kwargs)
            if result is not None and kwargs.get("with_for_update"):
                await after_lock(entity)
            return result

        self.monkeypatch.setattr(session, "execute", execute_with_gate)
        self.monkeypatch.setattr(session, "get", get_with_gate)
        if label == "delivery":
            self.delivery_started.set()

    async def blocking_edges(self, observer):
        rows = (await bounded(observer.execute(text(
            "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
            "WHERE pid IN (:cancel, :delivery)"
        ), self.pids))).all()
        for row in rows:
            for blocker in row.blockers:
                if blocker in self.pids.values() and [row.pid, blocker] not in self.edges:
                    self.edges.append([row.pid, blocker])
        return self.edges

    async def coordinate(self, cancel_task, delivery_task, *, allow_delivery_skip=False):
        await bounded(self.delivery_started.wait(), timeout=5)
        async with self.database() as observer:
            observer_pid = await bounded(observer.scalar(text("SELECT pg_backend_pid()")))
            assert observer_pid not in self.pids.values()
            async with asyncio.timeout(5):
                while not self.delivery_holds_offer.is_set():
                    await self.blocking_edges(observer)
                    if [self.pids["delivery"], self.pids["cancel"]] in self.edges:
                        # Correct request-first delivery is blocked before it
                        # can own an offer. Let cancellation commit first.
                        self.release_cancel.set()
                        self.release_delivery.set()
                        return
                    if allow_delivery_skip and delivery_task.done():
                        # A delayed scheduler may safely skip the locked
                        # request without ever owning its offer.
                        self.release_cancel.set()
                        self.release_delivery.set()
                        return
                    assert not delivery_task.done(), "Delivery exited without reaching its request/offer lock"
                    await asyncio.sleep(0.01)
            # Legacy delivery owns offer while cancellation owns request.
            # First prove cancellation really waits for that offer; only then
            # let delivery reach its request UPDATE/autoflush to close the cycle.
            self.release_cancel.set()
            async with asyncio.timeout(5):
                while [self.pids["cancel"], self.pids["delivery"]] not in self.edges:
                    await self.blocking_edges(observer)
                    assert not cancel_task.done(), "Cancellation did not block on the locked offer"
                    await asyncio.sleep(0.01)
            self.release_delivery.set()
            async with asyncio.timeout(5):
                while not any(task.done() for task in (cancel_task, delivery_task)):
                    await self.blocking_edges(observer)
                    if [self.pids["delivery"], self.pids["cancel"]] in self.edges:
                        return
                    await asyncio.sleep(0.01)


async def run_cancel_delivery(database, monkeypatch, operation, record_property, *, allow_delivery_skip=False):
    race = DeliveryCancelRace(database, monkeypatch)

    @asynccontextmanager
    async def delivery_sessions():
        async with database() as session:
            await race.prepare(session, "delivery")
            try:
                yield session
                assert await bounded(session.scalar(text("SELECT 1"))) == 1
            except DBAPIError:
                await bounded(session.rollback())
                assert await bounded(session.scalar(text("SELECT 1"))) == 1
                raise

    async def cancel():
        async with database() as session:
            await race.prepare(session, "cancel")
            try:
                result = await WaitlistService().cancel(session, CANCEL_TOKEN)
                assert await bounded(session.scalar(text("SELECT 1"))) == 1
                return {"operation": "cancel", "request_id": result.id}
            except DBAPIError as error:
                await bounded(session.rollback())
                assert await bounded(session.scalar(text("SELECT 1"))) == 1
                return {"operation": "cancel", "sqlstate": getattr(error.orig, "sqlstate", None)}

    async def deliver():
        try:
            return await operation(delivery_sessions)
        except DBAPIError as error:
            return {"operation": "delivery", "sqlstate": getattr(error.orig, "sqlstate", None)}

    cancel_task = asyncio.create_task(cancel())
    delivery_task = None
    try:
        await bounded(race.cancel_holds_request.wait(), timeout=5)
        delivery_task = asyncio.create_task(deliver())
        await race.coordinate(cancel_task, delivery_task, allow_delivery_skip=allow_delivery_skip)
        outcomes = await bounded(asyncio.gather(cancel_task, delivery_task))
    finally:
        race.release_cancel.set()
        race.release_delivery.set()
        tasks = [task for task in (cancel_task, delivery_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    record_property("backend_pids", json.dumps(race.pids, sort_keys=True))
    record_property("blocking_edges", json.dumps(race.edges))
    record_property("database_outcomes", json.dumps(outcomes, sort_keys=True))
    database_errors = [outcome for outcome in outcomes if "sqlstate" in outcome]
    if database_errors:
        assert all(outcome["sqlstate"] == "40P01" for outcome in database_errors), outcomes
    assert database_errors == [], f"Actual PostgreSQL deadlock, not a Python timeout: {outcomes}"


async def configure_cancel(database, context, *, offered):
    async with database() as session:
        request = await bounded(session.get(WaitlistRequest, context["request_id"]))
        request.cancel_token_hash = WaitlistService._hash_token(CANCEL_TOKEN)
        request.status = WaitlistStatus.offered if offered else WaitlistStatus.active
        await bounded(session.commit())


async def assert_cancelled_state(database, context):
    async with database() as session:
        request = await bounded(session.get(WaitlistRequest, context["request_id"]))
        offer = await bounded(session.get(WaitlistOffer, context["offer_id"]))
        assert (request.status, request.close_reason) == (WaitlistStatus.cancelled, "cancelled_by_customer")
        assert (offer.status, offer.close_reason) == (WaitlistOfferStatus.cancelled, "request_cancelled_by_customer")
        assert offer.sent_at is None
        assert list(await bounded(session.scalars(select(MessageRecipient)))) == []
        assert list(await bounded(session.scalars(select(MessageLog)))) == []
        assert await bounded(session.scalar(text("SELECT 1"))) == 1


@pytest.mark.parametrize("outcome", ["accepted", "failed"])
async def test_waitlist_outcome_and_customer_cancel_share_request_first_order_without_deadlock(
    database, monkeypatch, sms_transport, record_property, outcome,
):
    clock = SimulatedClock()
    context = await seed_waitlist(database, clock)
    _queue, initial_job = await waitlist_queue(database, monkeypatch, clock, context)
    await configure_cancel(database, context, offered=outcome == "failed")
    async with database() as session:
        job = await bounded(session.get(SmsQueueJob, initial_job.id))
        job.status = outcome
        job.attempts = 1
        job.transport_started_at = clock.value
        job.provider_message_id = "synthetic-accepted-message" if outcome == "accepted" else None
        job.error_code = None if outcome == "accepted" else "synthetic_provider_rejected"
        await bounded(session.commit())
        before_job = tuple((await bounded(session.execute(
            select(SmsQueueJob.__table__).where(SmsQueueJob.id == job.id),
        ))).one())

    async def project(delivery_sessions):
        monkeypatch.setattr(sms_notification_queue, "AsyncSessionLocal", delivery_sessions)
        await sms_notification_queue.sms_job_outcome(job)
        return {"operation": "delivery", "outcome": outcome}

    await run_cancel_delivery(database, monkeypatch, project, record_property)
    monkeypatch.setattr(sms_notification_queue, "AsyncSessionLocal", database)
    await bounded(sms_notification_queue.sms_job_outcome(job))
    await assert_cancelled_state(database, context)
    async with database() as session:
        after_job = tuple((await bounded(session.execute(
            select(SmsQueueJob.__table__).where(SmsQueueJob.id == job.id),
        ))).one())
        assert after_job == before_job
    assert sms_transport.requests == []


async def test_delayed_waitlist_sender_slot_failure_and_cancel_do_not_invert_request_offer_locks(
    database, monkeypatch, sms_transport, record_property,
):
    clock = SimulatedClock()
    context = await seed_waitlist(database, clock)
    await configure_cancel(database, context, offered=True)
    async with database() as session:
        offer = await bounded(session.get(WaitlistOffer, context["offer_id"]))
        offer.start_at = clock.value + timedelta(minutes=1)
        offer.end_at = clock.value + timedelta(minutes=31)
        await bounded(session.commit())
    queue = SmsQueueService(session_factory=database, clock=clock)
    service = WaitlistOfferService(sms_service=SmsService(queue=queue))

    async def send_due(delivery_sessions):
        async with delivery_sessions() as session:
            count = await service.send_due_offers(session, now=clock.value)
            assert count == 0
            assert await bounded(session.scalar(text("SELECT 1"))) == 1
            return {"operation": "delivery", "sent": count}

    await run_cancel_delivery(database, monkeypatch, send_due, record_property, allow_delivery_skip=True)
    await assert_cancelled_state(database, context)
    assert sms_transport.requests == []
