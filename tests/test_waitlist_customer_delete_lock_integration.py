"""Real waitlist/customer-delete FK lock races in isolated PostgreSQL.

The customer has only waitlist history, so the real deletion service reaches
its DELETE and PostgreSQL's RESTRICT check. Engine events gate real row locks;
timeouts only bound failures and never substitute for native SQLSTATE evidence.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.util.concurrency import await_only

from app.models.booking import Booking
from app.models.customer import Customer
from app.models.messaging import MessageDeliveryStatus, MessageLog, MessageRecipient
from app.models.order import Order
from app.models.sms_queue import SmsQueueJob
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.services import sms_notification_queue
from app.services.customer_auth import CustomerAuthService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_sms_business_final_admission_integration import seed_waitlist, waitlist_queue
from tests.test_sms_queue_integration import SimulatedClock


pytestmark = pytest.mark.anyio


async def bounded(operation, seconds=10):
    return await asyncio.wait_for(operation, seconds)


def database_error(error):
    native = getattr(error, "orig", error)
    return {
        "sqlstate": getattr(native, "sqlstate", None) or getattr(native, "pgcode", None),
        "constraint": getattr(getattr(native, "__cause__", None), "constraint_name", None),
    }


class CustomerDeleteRace:
    def __init__(self, database):
        self.database = database
        self.engine = database.kw["bind"].sync_engine
        self.pids = {}
        self.sql = []
        self.errors = []
        self.edges = []
        self.delivery_holds_request = asyncio.Event()
        self.delete_holds_customer = asyncio.Event()
        self.delete_attempted = asyncio.Event()
        self.release_delivery = asyncio.Event()
        self.release_delete = asyncio.Event()

    async def prepare(self, session, role):
        await session.execute(text("SET LOCAL lock_timeout = '10s'"))
        await session.execute(text("SET LOCAL statement_timeout = '15s'"))
        self.pids[role] = await session.scalar(text("SELECT pg_backend_pid()"))
        assert len(set(self.pids.values())) == len(self.pids)
        connection = await session.connection()
        connection.sync_connection.info["waitlist_customer_delete_role"] = role

    @staticmethod
    def normalized(statement):
        return " ".join(statement.replace('"', '').upper().split())

    def before_sql(self, connection, cursor, statement, parameters, context, executemany):
        role = connection.info.get("waitlist_customer_delete_role")
        sql = self.normalized(statement)
        if role not in {"delivery", "delete"}:
            return
        if "FOR " in sql or sql.startswith(("DELETE ", "INSERT ", "UPDATE ")):
            self.sql.append({"role": role, "phase": "before", "sql": sql})
        if role == "delete" and sql.startswith("DELETE FROM CUSTOMERS "):
            self.delete_attempted.set()

    def after_sql(self, connection, cursor, statement, parameters, context, executemany):
        role = connection.info.get("waitlist_customer_delete_role")
        sql = self.normalized(statement)
        if (role == "delivery" and "FROM WAITLIST_REQUESTS " in sql and "FOR " in sql
                and not self.delivery_holds_request.is_set()):
            assert cursor.rowcount == 1, "The gate must follow a real request row lock"
            self.delivery_holds_request.set()
            await_only(bounded(self.release_delivery.wait()))
        elif (role == "delete" and "FROM CUSTOMERS " in sql and "FOR UPDATE" in sql
                and not self.delete_holds_customer.is_set()):
            assert cursor.rowcount == 1, "Deletion must own the real customer row"
            self.delete_holds_customer.set()
            await_only(bounded(self.release_delete.wait()))

    def handle_error(self, context):
        self.errors.append(database_error(context.original_exception))

    def install(self):
        event.listen(self.engine, "before_cursor_execute", self.before_sql)
        event.listen(self.engine, "after_cursor_execute", self.after_sql)
        event.listen(self.engine, "handle_error", self.handle_error)

    async def observe_edges(self, observer):
        rows = (await observer.execute(text(
            "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
            "WHERE pid IN (:delivery, :delete)"
        ), self.pids)).all()
        for row in rows:
            for blocker in row.blockers:
                edge = [row.pid, blocker]
                if blocker in self.pids.values() and edge not in self.edges:
                    self.edges.append(edge)

    async def coordinate(self, delivery_task, delete_task):
        await bounded(self.delete_holds_customer.wait(), seconds=5)
        self.release_delete.set()
        await bounded(self.delete_attempted.wait(), seconds=5)
        async with self.database() as observer:
            assert await observer.scalar(text("SELECT pg_backend_pid()")) not in self.pids.values()
            # FOR UPDATE requests block the actual parent DELETE's FK probe.
            # NO KEY UPDATE permits that probe to reject with 23503 and roll back.
            async with asyncio.timeout(5):
                while not delete_task.done():
                    await self.observe_edges(observer)
                    if [self.pids["delete"], self.pids["delivery"]] in self.edges:
                        break
                    assert not delivery_task.done(), "Delivery escaped its request gate"
                    await asyncio.sleep(0.01)
            self.release_delivery.set()
            if not delete_task.done():
                # Close the native cycle only after proving DELETE waits on the
                # request. A fast server deadlock detector can resolve it before
                # both edges are sampled; native 40P01 is captured independently.
                async with asyncio.timeout(5):
                    while not any(task.done() for task in (delivery_task, delete_task)):
                        await self.observe_edges(observer)
                        if [self.pids["delivery"], self.pids["delete"]] in self.edges:
                            break
                        await asyncio.sleep(0.01)

    async def close(self, tasks):
        self.release_delivery.set()
        self.release_delete.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(self.engine, "before_cursor_execute", self.before_sql)
        event.remove(self.engine, "after_cursor_execute", self.after_sql)
        event.remove(self.engine, "handle_error", self.handle_error)


@pytest.mark.parametrize("path", ["admission", "outcome"])
async def test_waitlist_delivery_and_actual_customer_delete_preserve_restrict_without_deadlock(
    database, monkeypatch, record_property, path,
):
    clock = SimulatedClock()
    context = await seed_waitlist(database, clock)
    queue, initial_job = await waitlist_queue(database, monkeypatch, clock, context)
    async with database() as setup:
        assert await setup.scalar(select(func.count()).select_from(Order)) == 0
        assert await setup.scalar(select(func.count()).select_from(Booking)) == 0
        assert await setup.scalar(select(func.count()).select_from(MessageRecipient)) == 0
        constraint = (await setup.execute(text(
            "SELECT conname, confdeltype::text AS confdeltype, condeferrable FROM pg_constraint "
            "WHERE conrelid='waitlist_requests'::regclass "
            "AND confrelid='customers'::regclass AND contype='f'"
        ))).one()
        assert constraint.confdeltype == "r" and not constraint.condeferrable
        constraint_name = constraint.conname
        job = await setup.get(SmsQueueJob, initial_job.id)
        job.status = "dispatching" if path == "admission" else "accepted"
        job.attempts = 1
        if path == "admission":
            job.claimed_at = clock.value
            job.lease_token = "synthetic-customer-delete-lease"
            job.lease_expires_at = clock.value + timedelta(minutes=5)
        else:
            job.transport_started_at = clock.value
            job.accepted_at = clock.value
            job.provider_message_id = "synthetic-customer-delete-message"
        await setup.commit()
        before_job = tuple((await setup.execute(
            select(SmsQueueJob.__table__).where(SmsQueueJob.id == job.id)
        )).one())

    race = CustomerDeleteRace(database)

    @asynccontextmanager
    async def delivery_sessions():
        async with database() as session:
            await race.prepare(session, "delivery")
            try:
                yield session
            except DBAPIError:
                await session.rollback()
                assert await session.scalar(text("SELECT 1")) == 1
                raise

    async def deliver():
        try:
            if path == "admission":
                decision = await queue._admit_transport(job)
                return {"operation": path, "decision": None if decision is None else decision.action}
            await sms_notification_queue.sms_job_outcome(job)
            return {"operation": path, "projected": True}
        except DBAPIError as error:
            return {"operation": path, **database_error(error)}

    async def delete_customer():
        async with database() as session:
            await race.prepare(session, "delete")
            customer = await session.get(Customer, context["customer_id"])
            try:
                await CustomerAuthService().delete_customer(session, customer)
                return {"operation": "delete", "deleted": True}
            except DBAPIError as error:
                result = {"operation": "delete", **database_error(error)}
                await session.rollback()
                assert await session.scalar(text("SELECT 1")) == 1
                return result

    queue.session_factory = delivery_sessions
    monkeypatch.setattr(sms_notification_queue, "AsyncSessionLocal", delivery_sessions)
    race.install()
    tasks = [asyncio.create_task(deliver())]
    try:
        await bounded(race.delivery_holds_request.wait(), seconds=5)
        tasks.append(asyncio.create_task(delete_customer()))
        await race.coordinate(*tasks)
        outcomes = await bounded(asyncio.gather(*tasks), seconds=20)
        diagnostics = {
            "path": path, "pids": race.pids, "blocking_edges": race.edges,
            "sql": race.sql, "database_errors": race.errors, "outcomes": outcomes,
            "restrict_constraint": constraint_name,
        }
        record_property("waitlist_customer_delete_race", json.dumps(diagnostics, sort_keys=True))
        assert len(set(race.pids.values())) == 2
        assert race.delete_attempted.is_set(), "Actual customer deletion never reached its hard-delete branch"
        assert all(error["sqlstate"] in {"23503", "40P01"} for error in race.errors), diagnostics
        assert not any(error["sqlstate"] == "40P01" for error in race.errors), (
            "Native PostgreSQL request/customer FK deadlock, not a timeout", diagnostics,
        )
        assert outcomes[1] == {"operation": "delete", "sqlstate": "23503", "constraint": constraint_name}, diagnostics
        assert outcomes[0] == ({"operation": "admission", "decision": None} if path == "admission"
                               else {"operation": "outcome", "projected": True}), diagnostics
        async with database() as verify:
            customer = await verify.get(Customer, context["customer_id"])
            request = await verify.get(WaitlistRequest, context["request_id"])
            offer = await verify.get(WaitlistOffer, context["offer_id"])
            persisted = await verify.get(SmsQueueJob, job.id)
            assert customer is not None and customer.is_active
            assert request.customer_id == customer.id and offer.request_id == request.id
            recipients = list(await verify.scalars(select(MessageRecipient)))
            logs = list(await verify.scalars(select(MessageLog)))
            if path == "admission":
                assert persisted.status == "dispatching" and persisted.transport_started_at == clock.value
                assert persisted.attempts == 1 and persisted.provider_message_id is None
                assert request.status == WaitlistStatus.active and offer.status == WaitlistOfferStatus.pending
                assert recipients == logs == []
            else:
                after_job = tuple((await verify.execute(
                    select(SmsQueueJob.__table__).where(SmsQueueJob.id == job.id)
                )).one())
                assert after_job == before_job
                assert request.status == WaitlistStatus.offered and offer.status == WaitlistOfferStatus.sent
                assert offer.provider_message_id == "synthetic-customer-delete-message"
                assert len(recipients) == len(logs) == 1
                assert recipients[0].customer_id == customer.id and recipients[0].status == MessageDeliveryStatus.sent
                assert recipients[0].waitlist_offer_id == offer.id and recipients[0].waitlist_request_id == request.id
            assert await verify.scalar(select(func.count()).select_from(Order)) == 0
            assert await verify.scalar(select(func.count()).select_from(Booking)) == 0
    finally:
        await race.close(tasks)
