"""Real PostgreSQL admission locks; the only provider boundary is synthetic.

The review case characterizes the recipient-first SQL/FK protocol, rather than
the entire scheduler. UPDATE/INSERT and transport-start gates follow real SQL.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import json

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.util.concurrency import await_only

from app.models.customer import Customer
from app.models.messaging import (
    Campaign, ClientCommunicationPreference, MessageDeliveryStatus, MessagePurpose,
    MessageRecipient, ReviewPlatform, ReviewRequest,
)
from app.models.sms_queue import SmsAccountThrottle, SmsQueueJob
from app.services.messaging import MessagingService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_segments_integration import add_booking
from tests.test_sms_business_final_admission_integration import (
    assert_skipped_job, campaign_queue, seed_campaign,
)
from tests.test_sms_queue_integration import SimulatedClock


pytestmark = pytest.mark.anyio


async def bounded(operation, seconds=10):
    return await asyncio.wait_for(operation, seconds)


class AdmissionRace:
    def __init__(self, database):
        self.database = database
        self.engine = database.kw["bind"].sync_engine
        self.pids = {}
        self.sql = []
        self.edges = []
        self.errors = []
        self.probe_pids = []
        self.eligible = asyncio.Event()
        self.proceed = asyncio.Event()
        self.admission_entered = asyncio.Event()
        self.preference_written = asyncio.Event()
        self.release_preference = asyncio.Event()
        self.transport_written = asyncio.Event()
        self.release_transport = asyncio.Event()
        self.admission_committed = asyncio.Event()
        self.customer_locked = asyncio.Event()
        self.writer_attempted = asyncio.Event()
        self.hold_preference = False
        self.hold_transport = False
        self.preference_gate_used = False
        self.transport_gate_used = False
        self.session_listeners = []

    async def tag(self, session, role):
        await session.execute(text("SET LOCAL lock_timeout = '10s'"))
        await session.execute(text("SET LOCAL statement_timeout = '15s'"))
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        if role in self.pids:
            assert self.pids[role] == pid
        self.pids[role] = pid
        assert len(set(self.pids.values())) == len(self.pids)
        connection = await session.connection()
        connection.sync_connection.info["sms_admission_role"] = role

    def before_sql(self, connection, cursor, statement, parameters, context, executemany):
        role = connection.info.get("sms_admission_role")
        sql = " ".join(statement.replace('"', '').upper().split())
        if role == "preference" and "FROM CUSTOMERS " in sql and "FOR " in sql:
            self.writer_attempted.set()

    def after_sql(self, connection, cursor, statement, parameters, context, executemany):
        role = connection.info.get("sms_admission_role")
        sql = " ".join(statement.replace('"', '').upper().split())
        if role not in {"admission", "preference", "review"}:
            return
        if "FOR " in sql or sql.startswith(("UPDATE ", "INSERT ")):
            self.sql.append(dict(role=role, sql=sql, rowcount=cursor.rowcount))
        if role == "admission" and "FROM CUSTOMERS " in sql and "FOR " in sql:
            assert "FOR NO KEY UPDATE" in sql, sql
            self.customer_locked.set()
        if (role == "preference" and self.hold_preference and not self.preference_gate_used
                and sql.startswith(("UPDATE CLIENT_COMMUNICATION_PREFERENCES ",
                                    "INSERT INTO CLIENT_COMMUNICATION_PREFERENCES "))):
            self.preference_gate_used = True
            self.preference_written.set()
            await_only(bounded(self.release_preference.wait()))
        if (role == "admission" and self.hold_transport and not self.transport_gate_used
                and sql.startswith("UPDATE SMS_QUEUE_JOBS ") and "TRANSPORT_STARTED_AT" in sql):
            assert cursor.rowcount == 1, sql
            self.transport_gate_used = True
            self.transport_written.set()
            await_only(bounded(self.release_transport.wait()))

    def handle_error(self, context):
        error = context.original_exception
        state = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        self.errors.append(dict(sqlstate=state, error=str(error)))

    def install(self, queue):
        event.listen(self.engine, "before_cursor_execute", self.before_sql)
        event.listen(self.engine, "after_cursor_execute", self.after_sql)
        event.listen(self.engine, "handle_error", self.handle_error)
        real_eligibility = queue.before_dispatch
        real_context = queue._lock_admission_context

        async def pause_after_real_eligibility(job):
            decision = await real_eligibility(job)
            assert decision.action == "allow", decision
            self.eligible.set()
            await bounded(self.proceed.wait())
            return decision

        async def trace_real_context(session, job):
            await self.tag(session, "admission")
            event.listen(session.sync_session, "after_commit", self.after_admission_commit)
            self.session_listeners.append(session.sync_session)
            self.admission_entered.set()
            return await real_context(session, job)

        queue.before_dispatch = pause_after_real_eligibility
        queue._lock_admission_context = trace_real_context

    def after_admission_commit(self, _session):
        self.admission_committed.set()

    async def observe(self, waiter, holder, tasks):
        assert self.pids[waiter] != self.pids[holder]
        async with self.database() as observer:
            observer_pid = await observer.scalar(text("SELECT pg_backend_pid()"))
            assert observer_pid not in self.pids.values()
            async with asyncio.timeout(5):
                while True:
                    blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"),
                                                     {"pid": self.pids[waiter]})
                    if self.pids[holder] in blockers:
                        self.edges.append([self.pids[waiter], self.pids[holder]])
                        return
                    assert not any(task.done() for task in tasks), (
                        "Operation completed before the required real blocking edge", self.sql, self.errors,
                    )
                    await asyncio.sleep(0.01)

    async def close(self, tasks):
        self.proceed.set()
        self.release_preference.set()
        self.release_transport.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(self.engine, "before_cursor_execute", self.before_sql)
        event.remove(self.engine, "after_cursor_execute", self.after_sql)
        event.remove(self.engine, "handle_error", self.handle_error)
        for session in self.session_listeners:
            event.remove(session, "after_commit", self.after_admission_commit)

    def record(self, record_property):
        record_property("sms_admission_locks", json.dumps(dict(
            pids=self.pids, probe_pids=self.probe_pids, blocking_edges=self.edges,
            sql=self.sql, database_errors=self.errors,
            admission_committed=self.admission_committed.is_set(),
        ), sort_keys=True))


async def setup_campaign(database, *, missing=False, purpose=MessagePurpose.transactional):
    clock = SimulatedClock()
    context = await seed_campaign(database, clock, purpose=purpose)
    if missing:
        async with database() as session:
            preference = (await session.scalars(select(ClientCommunicationPreference))).one()
            await session.delete(preference)
            await session.commit()
    queue, job = await campaign_queue(database, clock, context)
    return clock, context, queue, job


async def update_preference(database, race, context, *, done=None):
    async with database() as session:
        await race.tag(session, "preference")
        result = await MessagingService().upsert_preference(
            session, context["customer_id"], {"do_not_contact": True},
        )
        assert result.do_not_contact is True
        assert await session.scalar(text("SELECT 1")) == 1
    if done is not None:
        done.set()


@pytest.mark.parametrize("missing", [False, True], ids=["existing-preference", "missing-preference"])
async def test_committing_actual_preference_upsert_first_blocks_admission_and_skips(
    database, missing, record_property,
):
    _, context, queue, job = await setup_campaign(database, missing=missing)
    race = AdmissionRace(database)
    race.hold_preference = True
    race.install(queue)
    provider_calls = []

    async def provider(claimed):
        provider_calls.append(claimed.id)
        return {"success_request": {"info": {"synthetic": context["phone"]}}}

    queue.transport = provider
    tasks = [asyncio.create_task(queue.process_one())]
    try:
        await bounded(race.eligible.wait())
        tasks.append(asyncio.create_task(update_preference(database, race, context)))
        await bounded(race.preference_written.wait())
        assert not race.admission_committed.is_set()
        race.proceed.set()
        await bounded(race.admission_entered.wait())
        await race.observe("admission", "preference", tasks)
        assert provider_calls == []
        race.release_preference.set()
        assert await bounded(tasks[0]) is True
        await bounded(tasks[1])
        assert provider_calls == []
        await assert_skipped_job(database, job.id, "Client is marked do-not-contact")
        async with database() as session:
            preference = (await session.scalars(select(ClientCommunicationPreference))).one()
            recipient = await session.get(MessageRecipient, context["recipient_id"])
            assert preference.customer_id == context["customer_id"] and preference.do_not_contact
            assert recipient.status == MessageDeliveryStatus.skipped
        assert race.errors == [], race.errors
        race.record(record_property)
    finally:
        await race.close(tasks)


async def assert_admission_locks_released(database, race, context, job):
    # Commit releases connections to the pool. Keep any reused prior backends
    # checked out until the probe demonstrably owns a third backend.
    async with AsyncExitStack() as stack:
        for _ in range(len(race.pids) + 1):
            probe = await stack.enter_async_context(database())
            pid = await probe.scalar(text("SELECT pg_backend_pid()"))
            if pid not in race.pids.values():
                break
        assert pid not in race.pids.values()
        race.probe_pids.append(pid)
        connection = await probe.connection()
        connection.sync_connection.info["sms_admission_role"] = "probe"
        rows = [
            select(Campaign).where(Campaign.id == context["campaign_id"]),
            select(Customer).where(Customer.id == context["customer_id"]),
            select(ClientCommunicationPreference).where(
                ClientCommunicationPreference.customer_id == context["customer_id"]),
            select(MessageRecipient).where(MessageRecipient.id == context["recipient_id"]),
            select(SmsAccountThrottle).where(SmsAccountThrottle.account_key == job.account_key),
            select(SmsQueueJob).where(SmsQueueJob.id == job.id),
        ]
        for statement in rows:
            assert await probe.scalar(statement.with_for_update(nowait=True)) is not None
        persisted = await probe.get(SmsQueueJob, job.id)
        assert persisted.transport_started_at is not None and persisted.status == "dispatching"
        await probe.rollback()


async def test_admission_commit_first_blocks_upsert_and_releases_all_locks_before_provider(
    database, record_property,
):
    _, context, queue, job = await setup_campaign(database)
    race = AdmissionRace(database)
    race.hold_transport = True
    race.install(queue)
    preference_done = asyncio.Event()
    provider_checks = []

    async def provider(claimed):
        assert race.admission_committed.is_set()
        await bounded(preference_done.wait())
        await assert_admission_locks_released(database, race, context, job)
        provider_checks.append(claimed.id)
        return {"success_request": {"info": {"synthetic-admitted": context["phone"]}}}

    queue.transport = provider
    tasks = [asyncio.create_task(queue.process_one())]
    try:
        await bounded(race.eligible.wait())
        race.proceed.set()
        await bounded(race.transport_written.wait())
        assert race.customer_locked.is_set() and not race.admission_committed.is_set()
        tasks.append(asyncio.create_task(update_preference(database, race, context, done=preference_done)))
        await bounded(race.writer_attempted.wait())
        await race.observe("preference", "admission", tasks)
        assert provider_checks == []
        race.release_transport.set()
        assert await bounded(tasks[0]) is True
        await bounded(tasks[1])
        assert provider_checks == [job.id]
        async with database() as session:
            persisted = await session.get(SmsQueueJob, job.id)
            assert (persisted.status, persisted.attempts, persisted.provider_message_id) == (
                "accepted", 1, "synthetic-admitted",
            )
            assert persisted.transport_started_at is not None
            preference = (await session.scalars(select(ClientCommunicationPreference))).one()
            assert preference.do_not_contact
        assert race.errors == [], race.errors
        race.record(record_property)
    finally:
        await race.close(tasks)


async def test_recipient_first_review_fk_insert_completes_under_admission_customer_no_key_update(
    database, record_property,
):
    clock, context, queue, job = await setup_campaign(database, purpose=MessagePurpose.review_request)
    async with database() as session:
        customer = await session.get(Customer, context["customer_id"])
        booking = await add_booking(session, customer, clock.value - timedelta(hours=1))
        await session.commit()
        booking_id, master_id = booking.id, booking.master_id
        # Successful INSERT must exercise an immediate real Customer FK.
        assert await session.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conrelid='review_requests'::regclass "
            "AND confrelid='customers'::regclass AND contype='f' AND NOT condeferrable"
        )) == 1
    race = AdmissionRace(database)
    race.install(queue)
    recipient_locked, insert_review = asyncio.Event(), asyncio.Event()
    inserted = []
    provider_calls = []

    async def recipient_first_writer():
        async with database() as session:
            await race.tag(session, "review")
            recipient = await session.get(MessageRecipient, context["recipient_id"], with_for_update=True)
            assert recipient is not None
            recipient_locked.set()
            await bounded(insert_review.wait())
            request = ReviewRequest(
                campaign_id=context["campaign_id"], appointment_id=booking_id,
                customer_id=context["customer_id"], master_id=master_id,
                recipient_id=recipient.id, platform=ReviewPlatform.internal,
                review_url="/synthetic-review", scheduled_at=clock.value,
            )
            session.add(request)
            await session.flush()
            inserted.append(request.id)
            await session.commit()

    async def provider(claimed):
        assert race.admission_committed.is_set() and inserted
        provider_calls.append(claimed.id)
        return {"success_request": {"info": {"synthetic-review": context["phone"]}}}

    queue.transport = provider
    tasks = [asyncio.create_task(queue.process_one())]
    try:
        await bounded(race.eligible.wait())
        tasks.append(asyncio.create_task(recipient_first_writer()))
        await bounded(recipient_locked.wait())
        race.proceed.set()
        await bounded(race.customer_locked.wait())
        await race.observe("admission", "review", tasks)
        assert inserted == [] and provider_calls == []
        insert_review.set()
        await bounded(tasks[1])
        assert await bounded(tasks[0]) is True
        assert len(inserted) == 1 and provider_calls == [job.id]
        async with database() as session:
            request = await session.get(ReviewRequest, inserted[0])
            assert (request.customer_id, request.recipient_id, request.appointment_id) == (
                context["customer_id"], context["recipient_id"], booking_id,
            )
            persisted = await session.get(SmsQueueJob, job.id)
            assert persisted.status == "accepted" and persisted.provider_message_id == "synthetic-review"
        assert race.errors == [], race.errors
        race.record(record_property)
    finally:
        insert_review.set()
        await race.close(tasks)
