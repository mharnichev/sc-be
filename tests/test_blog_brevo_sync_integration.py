"""Real subscription transactions versus a fully synthetic external contact.

The native baseline permits reversed provider completion. An ordered sync path
must instead prove its advisory wait after the newer local commit, then proceed.
No historical subscription is automatically synchronized by these fixtures.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import threading
from urllib.error import URLError
from urllib.parse import unquote

import pytest
from sqlalchemy import event, select, text

from app.core.config import settings
from app.models.blog import (
    BlogSubscription, BlogSubscriptionEvent, BlogSubscriptionEventType, BlogSubscriptionStatus,
)
from app.services import blog
from app.services.brevo import BrevoContactSyncService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


pytestmark = pytest.mark.anyio
ROLE = ContextVar("synthetic_brevo_role", default=None)


async def bounded(operation, seconds=10):
    return await asyncio.wait_for(operation, seconds)


class SyntheticContact:
    def __init__(self, email, *, blacklisted=False, fail=False):
        self.email = email
        self.blacklisted = blacklisted
        self.fail = fail
        self.calls = []
        self.loop = asyncio.get_running_loop()
        self.old_started = asyncio.Event()
        self.new_finished = asyncio.Event()
        self.release_old = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.guard = threading.Lock()
        self.active = 0

    def request(self, method, path, payload):
        role = ROLE.get()
        email = payload["email"] if method == "POST" else unquote(path.removeprefix("/contacts/"))
        assert email == self.email
        assert method in {"POST", "PUT"}
        expected = ({"email": email, "emailBlacklisted": False, "updateEnabled": True, "listIds": [42]}
                    if method == "POST" else {"emailBlacklisted": True, "unlinkListIds": [42]})
        assert payload == expected
        with self.guard:
            self.active += 1
            self.idle.clear()
            self.calls.append(dict(role=role, method=method, path=path, payload=deepcopy(payload), completed=False))
            call = self.calls[-1]
        try:
            if role == "old":
                self.loop.call_soon_threadsafe(self.old_started.set)
                assert self.release_old.wait(20), "Synthetic old HTTP request was not released"
            if self.fail:
                raise URLError("Synthetic transport rejected before changing contact")
            with self.guard:
                self.blacklisted = payload["emailBlacklisted"]
                call["completed"] = True
            if role == "new":
                self.loop.call_soon_threadsafe(self.new_finished.set)
            return {}
        finally:
            with self.guard:
                self.active -= 1
                if self.active == 0:
                    self.idle.set()


def install_provider(database, monkeypatch, contact):
    monkeypatch.setattr(settings, "brevo_sync_enabled", True)
    monkeypatch.setattr(settings, "brevo_api_key", "synthetic-key-never-used-on-network")
    monkeypatch.setattr(settings, "brevo_contact_list_id", 42)
    service = BrevoContactSyncService()
    # Supported future coordinator seam; old transport simply ignores it.
    monkeypatch.setattr(service, "session_factory", database, raising=False)
    monkeypatch.setattr(service, "_request_json", contact.request)
    monkeypatch.setattr(blog, "brevo_contact_sync_service", service)
    return service


async def seed_subscribed(database, email):
    first = datetime.now(UTC) - timedelta(days=2)
    async with database() as session:
        subscription = BlogSubscription(
            email=email, status=BlogSubscriptionStatus.subscribed,
            unsubscribe_token="synthetic-original-token", first_subscribed_at=first, subscribed_at=first,
            source="synthetic-seed", language="en", metadata_json={"seed": True},
        )
        session.add(subscription)
        await session.flush()
        session.add(BlogSubscriptionEvent(subscription_id=subscription.id,
                                         event_type=BlogSubscriptionEventType.subscribed,
                                         source=subscription.source, occurred_at=first, metadata_json={}))
        await session.commit()
        return subscription.id, subscription.unsubscribe_token, first


async def local_state(database, email):
    async with database() as session:
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        subscription = (await session.scalars(select(BlogSubscription).where(
            BlogSubscription.email == email))).one()
        events = list(await session.scalars(select(BlogSubscriptionEvent).where(
            BlogSubscriptionEvent.subscription_id == subscription.id).order_by(BlogSubscriptionEvent.id)))
        return subscription, events, pid


class AdvisoryTrace:
    def __init__(self, database):
        self.database = database
        self.engine = database.kw["bind"].sync_engine
        self.attempts = []
        self.granted = {}
        self.edges = []
        self.new_attempted = asyncio.Event()

    def before_sql(self, connection, _cursor, statement, _parameters, _context, _executemany):
        sql = " ".join(statement.upper().split())
        role = ROLE.get()
        if role in {"old", "new"} and "PG_" in sql and "ADVISORY" in sql and "UNLOCK" not in sql:
            pid = connection.connection.driver_connection.get_server_pid()
            self.attempts.append(dict(role=role, pid=pid, sql=sql))
            if role == "new":
                self.new_attempted.set()

    def after_sql(self, connection, _cursor, statement, _parameters, _context, _executemany):
        sql = " ".join(statement.upper().split())
        role = ROLE.get()
        if role in {"old", "new"} and "PG_" in sql and "ADVISORY" in sql and "UNLOCK" not in sql:
            self.granted[role] = connection.connection.driver_connection.get_server_pid()

    def install(self):
        event.listen(self.engine, "before_cursor_execute", self.before_sql)
        event.listen(self.engine, "after_cursor_execute", self.after_sql)

    def remove(self):
        event.remove(self.engine, "before_cursor_execute", self.before_sql)
        event.remove(self.engine, "after_cursor_execute", self.after_sql)

    async def prove_new_waits_for_old(self, task):
        await bounded(self.new_attempted.wait())
        old = self.granted["old"]
        new = next(item["pid"] for item in reversed(self.attempts) if item["role"] == "new")
        assert new != old
        async with self.database() as observer:
            assert await observer.scalar(text("SELECT pg_backend_pid()")) not in {old, new}
            async with asyncio.timeout(5):
                while True:
                    blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": new})
                    if old in blockers:
                        rows = (await observer.execute(text(
                            "SELECT pid, granted FROM pg_locks WHERE locktype='advisory' AND pid IN (:old, :new)"
                        ), {"old": old, "new": new})).all()
                        assert any(row.pid == old and row.granted for row in rows)
                        assert any(row.pid == new and not row.granted for row in rows)
                        self.edges.append([new, old])
                        return
                    assert not task.done(), "New sync returned without required real advisory serialization"
                    await asyncio.sleep(0.01)


@pytest.mark.parametrize("scenario", ["subscribe_then_unsubscribe", "unsubscribe_then_resubscribe"])
async def test_latest_committed_subscription_wins_over_late_old_brevo_callback(
    database, monkeypatch, scenario, record_property,
):
    email = f"synthetic-{scenario.replace('_', '-')}@example.test"
    contact = SyntheticContact(email)
    install_provider(database, monkeypatch, contact)
    seeded = await seed_subscribed(database, email) if scenario == "unsubscribe_then_resubscribe" else None
    service = blog.BlogSubscriptionService()
    trace = AdvisoryTrace(database)
    trace.install()
    committed = {role: asyncio.Event() for role in ("old", "new")}
    pids = {}

    async def operation(role):
        role_token = ROLE.set(role)
        try:
            async with database() as session:
                pids[role] = await session.scalar(text("SELECT pg_backend_pid()"))
                callback = lambda _session: committed[role].set()
                event.listen(session.sync_session, "after_commit", callback)
                try:
                    subscribing = (scenario == "subscribe_then_unsubscribe") == (role == "old")
                    personal_token = await session.scalar(select(BlogSubscription.unsubscribe_token).where(BlogSubscription.email == email))
                    if subscribing:
                        result = await service.subscribe(session, dict(
                            confirmation_token=personal_token, email=f"  {email.upper()}  ", name="Synthetic reader", source="synthetic-modal",
                            language="uk", utm_source="synthetic-source", metadata_json={"synthetic": True},
                        ), subscriber_ip="192.0.2.41", user_agent="Synthetic subscriber")
                    else:
                        result = await service.unsubscribe(
                            session, token=personal_token, email=f" {email.upper()} ", reason="Synthetic unsubscribe",
                            subscriber_ip="192.0.2.42", user_agent="Synthetic unsubscriber",
                        )
                    assert await session.scalar(text("SELECT 1")) == 1
                    return result.id
                finally:
                    event.remove(session.sync_session, "after_commit", callback)
        finally:
            ROLE.reset(role_token)

    tasks = [asyncio.create_task(operation("old"))]
    try:
        await bounded(contact.old_started.wait())
        assert committed["old"].is_set()
        old_local, old_events, observer_pid = await local_state(database, email)
        assert observer_pid != pids["old"]
        original = (old_local.id, old_local.unsubscribe_token, old_local.first_subscribed_at)
        if seeded is not None:
            assert original == seeded
        tasks.append(asyncio.create_task(operation("new")))
        await bounded(committed["new"].wait())
        newer, newer_events, observer_pid = await local_state(database, email)
        expected_status = (BlogSubscriptionStatus.unsubscribed if scenario == "subscribe_then_unsubscribe"
                           else BlogSubscriptionStatus.subscribed)
        expected_events = [BlogSubscriptionEventType.subscribed, BlogSubscriptionEventType.unsubscribed]
        if scenario == "unsubscribe_then_resubscribe":
            expected_events.append(BlogSubscriptionEventType.resubscribed)
        assert len(set(pids.values())) == 2 and observer_pid not in pids.values()
        assert newer.status == expected_status
        assert [item.event_type for item in newer_events] == expected_events
        assert (newer.id, newer.unsubscribe_token, newer.first_subscribed_at) == original
        assert len(newer_events) == len(old_events) + 1
        if "old" in trace.granted:
            # Future ordered path: new local state is already committed, but its
            # provider attempt must wait on the old attempt's actual DB mutex.
            await trace.prove_new_waits_for_old(tasks[1])
            assert not contact.new_finished.is_set()
        else:
            # Current path: let new remote state finish, then release the old
            # remote write. This produces divergence rather than a timing guess.
            await bounded(tasks[1])
            assert contact.new_finished.is_set()
            assert contact.blacklisted == (expected_status == BlogSubscriptionStatus.unsubscribed)
        contact.release_old.set()
        ids = await bounded(asyncio.gather(*tasks))
        final, final_events, _ = await local_state(database, email)
        assert ids == [final.id, final.id]
        assert final.status == expected_status
        assert [item.event_type for item in final_events] == expected_events
        assert (final.id, final.unsubscribe_token, final.first_subscribed_at) == original
        assert final.metadata_json == {"synthetic": True} and final.language == "uk"
        unsubscribe_event = next(item for item in final_events if item.event_type == BlogSubscriptionEventType.unsubscribed)
        assert unsubscribe_event.metadata_json == {"reason": "Synthetic unsubscribe"}
        assert unsubscribe_event.subscriber_ip == "192.0.2.42"
        assert unsubscribe_event.user_agent == "Synthetic unsubscriber"
        if expected_status == BlogSubscriptionStatus.subscribed:
            assert final.unsubscribe_reason is None and final.unsubscribed_at is None
        else:
            assert final.unsubscribe_reason == "Synthetic unsubscribe" and final.unsubscribed_at is not None
        record_property("brevo_ordering", json.dumps(dict(
            scenario=scenario, transaction_pids=pids, advisory_attempts=trace.attempts,
            blocking_edges=trace.edges, calls=contact.calls, local_status=final.status.value,
            provider_blacklisted=contact.blacklisted,
        ), sort_keys=True))
        assert contact.blacklisted == (expected_status == BlogSubscriptionStatus.unsubscribed), (
            "Late old Brevo callback overwrote the latest committed local state", contact.calls,
        )
    finally:
        contact.release_old.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        trace.remove()
        assert await asyncio.to_thread(contact.idle.wait, 5), "Synthetic transport thread did not finish"


async def test_failed_brevo_sync_preserves_local_unsubscribe_and_durable_pending_version(
    database, monkeypatch, record_property,
):
    email = "synthetic-durable-failure@example.test"
    contact = SyntheticContact(email, fail=True)
    install_provider(database, monkeypatch, contact)
    subscription_id, token, first = await seed_subscribed(database, email)
    async with database() as session:
        table = await session.scalar(text("SELECT to_regclass('blog_brevo_sync_jobs')::text"))
        if table is not None:
            assert await session.scalar(text("SELECT count(*) FROM blog_brevo_sync_jobs")) == 0
    async with database() as request_session:
        request_pid = await request_session.scalar(text("SELECT pg_backend_pid()"))
        result = await blog.BlogSubscriptionService().unsubscribe(
            request_session, token=token, reason="Synthetic provider failure",
            subscriber_ip="192.0.2.43", user_agent="Synthetic failure test",
        )
        assert result.id == subscription_id and result.status == BlogSubscriptionStatus.unsubscribed
        assert await request_session.scalar(text("SELECT 1")) == 1
        async with database() as verify:
            assert await verify.scalar(text("SELECT pg_backend_pid()")) != request_pid
            persisted = await verify.get(BlogSubscription, subscription_id)
            events = list(await verify.scalars(select(BlogSubscriptionEvent).where(
                BlogSubscriptionEvent.subscription_id == subscription_id).order_by(BlogSubscriptionEvent.id)))
            assert persisted.status == BlogSubscriptionStatus.unsubscribed
            assert (persisted.unsubscribe_token, persisted.first_subscribed_at) == (token, first)
            assert persisted.unsubscribe_reason == "Synthetic provider failure"
            assert [item.event_type for item in events] == [
                BlogSubscriptionEventType.subscribed, BlogSubscriptionEventType.unsubscribed,
            ]
            assert events[-1].metadata_json == {"reason": "Synthetic provider failure"}
            assert contact.blacklisted is False and len(contact.calls) == 1
            table = await verify.scalar(text("SELECT to_regclass('blog_brevo_sync_jobs')::text"))
            record_property("brevo_failure", json.dumps(dict(
                local_status=persisted.status.value, provider_blacklisted=contact.blacklisted,
                sync_table=table, calls=contact.calls,
            ), sort_keys=True))
            assert table is not None, "Failed external sync has no durable blog_brevo_sync_jobs table/intent"
            pending = (await verify.execute(text(
                "SELECT subscription_id, desired_version, synced_version FROM blog_brevo_sync_jobs "
                "WHERE subscription_id=:id"
            ), {"id": subscription_id})).one()
            assert pending.subscription_id == subscription_id
            assert pending.desired_version > pending.synced_version >= 0


from app.models.blog import BlogBrevoSyncJob
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def sync_job(database, subscription_id):
    async with database() as session:
        return await session.get(BlogBrevoSyncJob, subscription_id)


async def test_failed_intent_retries_latest_committed_state_once_after_restart(database, monkeypatch):
    email = "synthetic-retry-latest@example.test"
    contact = SyntheticContact(email, fail=True)
    install_provider(database, monkeypatch, contact)
    subscription_id, token, first = await seed_subscribed(database, email)
    async with database() as session:
        await blog.BlogSubscriptionService().unsubscribe(session, token=token, reason="Synthetic failure")
    failed = await sync_job(database, subscription_id)
    assert (failed.desired_version, failed.synced_version, failed.attempt_version, failed.attempts) == (1, 0, 1, 1)
    assert failed.last_error == "URLError" and failed.next_attempt_at > failed.last_attempt_at
    assert contact.calls[0]["method"] == "PUT" and not contact.calls[0]["completed"]

    # A newer local request replaces the failed intent; no stale PUT is replayed.
    monkeypatch.setattr(settings, "brevo_sync_enabled", False)
    async with database() as session:
        await blog.BlogSubscriptionService().subscribe(session, {"email": email, "source": "synthetic-retry", "confirmation_token": token})
    pending = await sync_job(database, subscription_id)
    assert (pending.desired_version, pending.synced_version, pending.attempts) == (2, 0, 0)
    monkeypatch.setattr(settings, "brevo_sync_enabled", True)
    contact.fail = False
    restarted = BrevoContactSyncService(session_factory=database)
    monkeypatch.setattr(restarted, "_request_json", contact.request)
    assert await restarted.sync_pending() == 1
    assert await restarted.sync_pending() == 0
    done = await sync_job(database, subscription_id)
    assert (done.desired_version, done.synced_version, done.attempt_version, done.attempts) == (2, 2, 2, 1)
    assert done.last_error is None and contact.blacklisted is False
    assert [call["method"] for call in contact.calls] == ["PUT", "POST"]
    final, events, _ = await local_state(database, email)
    assert (final.unsubscribe_token, final.first_subscribed_at) == (token, first)
    assert final.status == BlogSubscriptionStatus.subscribed and final.unsubscribe_reason is None
    assert [row.event_type for row in events] == [
        BlogSubscriptionEventType.subscribed, BlogSubscriptionEventType.unsubscribed,
        BlogSubscriptionEventType.resubscribed,
    ]


async def test_disabled_requests_queue_every_repeat_without_historical_autosync(database, monkeypatch):
    email = "synthetic-cold-repeats@example.test"
    contact = SyntheticContact(email)
    service = install_provider(database, monkeypatch, contact)
    legacy_id, _, _ = await seed_subscribed(database, "synthetic-untouched-legacy@example.test")
    monkeypatch.setattr(settings, "brevo_sync_enabled", False)
    transitions = blog.BlogSubscriptionService()
    async with database() as session:
        initial = await transitions.subscribe(session, {"email": email, "metadata_json": {"synthetic": True}})
        subscription_id, token, first = initial.id, initial.unsubscribe_token, initial.first_subscribed_at
        await transitions.subscribe(session, {"email": email})
        await transitions.unsubscribe(session, email=email, token=token, reason="First reason")
        await transitions.unsubscribe(session, token=token, reason="Repeated reason")
    pending = await sync_job(database, subscription_id)
    assert (pending.desired_version, pending.synced_version, pending.attempts) == (4, 0, 0)
    assert pending.attempt_version is None and pending.last_attempt_at is None
    assert await sync_job(database, legacy_id) is None
    assert await service.sync_pending() == 0 and contact.calls == []
    final, events, _ = await local_state(database, email)
    assert (final.unsubscribe_token, final.first_subscribed_at) == (token, first)
    assert final.unsubscribe_reason == "First reason" and final.metadata_json == {"synthetic": True}
    assert [row.event_type for row in events] == [
        BlogSubscriptionEventType.subscribed, BlogSubscriptionEventType.unsubscribed,
    ]
    monkeypatch.setattr(settings, "brevo_sync_enabled", True)
    restarted = BrevoContactSyncService(session_factory=database)
    monkeypatch.setattr(restarted, "_request_json", contact.request)
    assert await restarted.sync_pending(limit=0) == 0
    assert await restarted.sync_pending() == 1
    assert await restarted.sync_pending() == 0
    assert contact.blacklisted is True and [call["method"] for call in contact.calls] == ["PUT"]
    assert await sync_job(database, legacy_id) is None
    done = await sync_job(database, subscription_id)
    assert (done.desired_version, done.synced_version) == (4, 4)


async def test_provider_io_has_durable_attempt_but_no_business_row_locks_and_old_ack_cannot_clear_new_intent(
    database, monkeypatch, record_property,
):
    email = "synthetic-version-during-http@example.test"
    contact = SyntheticContact(email)
    sync = install_provider(database, monkeypatch, contact)
    trace = AdvisoryTrace(database)
    trace.install()

    async def old_operation():
        role = ROLE.set("old")
        try:
            async with database() as session:
                return await blog.BlogSubscriptionService().subscribe(session, {"email": email})
        finally:
            ROLE.reset(role)

    task = asyncio.create_task(old_operation())
    try:
        await bounded(contact.old_started.wait())
        persisted, _, _ = await local_state(database, email)
        attempted = await sync_job(database, persisted.id)
        assert (attempted.desired_version, attempted.synced_version, attempted.attempt_version, attempted.attempts) == (1, 0, 1, 1)
        owner = trace.granted["old"]
        async with database() as observer:
            observer_pid = await observer.scalar(text("SELECT pg_backend_pid()"))
            assert observer_pid != owner
            state = (await observer.execute(text(
                "SELECT state, xact_start FROM pg_stat_activity WHERE pid=:pid"
            ), {"pid": owner})).one()
            assert state.state == "idle" and state.xact_start is None
            for table, key in (("blog_subscriptions", "id"), ("blog_brevo_sync_jobs", "subscription_id")):
                assert await observer.scalar(text(
                    f"SELECT {key} FROM {table} WHERE {key}=:id FOR UPDATE NOWAIT"
                ), {"id": persisted.id}) == persisted.id
            await observer.rollback()
        # Commit a newer intent while HTTP is in flight, with provider delivery
        # temporarily disabled. The old completion must not acknowledge it.
        monkeypatch.setattr(settings, "brevo_sync_enabled", False)
        async with database() as session:
            await blog.BlogSubscriptionService().unsubscribe(session, email=email, token=persisted.unsubscribe_token, reason="New intent during HTTP")
        monkeypatch.setattr(settings, "brevo_sync_enabled", True)
        contact.release_old.set()
        await bounded(task)
        pending = await sync_job(database, persisted.id)
        assert (pending.desired_version, pending.synced_version, pending.attempts) == (2, 0, 0)
        assert contact.blacklisted is False
        assert await sync.sync_pending() == 1
        done = await sync_job(database, persisted.id)
        assert (done.desired_version, done.synced_version) == (2, 2)
        assert contact.blacklisted is True
        record_property("brevo_provider_boundary", json.dumps(dict(
            owner_pid=owner, observer_pid=observer_pid, state_during_http=state.state,
            durable_attempt=attempted.attempt_version, calls=contact.calls,
        ), sort_keys=True))
    finally:
        contact.release_old.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        trace.remove()
        assert await asyncio.to_thread(contact.idle.wait, 5)


async def test_cancellation_retains_advisory_ownership_until_live_http_thread_finishes(
    database, monkeypatch, record_property,
):
    email = "synthetic-cancellation@example.test"
    contact = SyntheticContact(email)
    install_provider(database, monkeypatch, contact)
    trace = AdvisoryTrace(database)
    trace.install()
    new_committed = asyncio.Event()

    async def operation(role):
        context = ROLE.set(role)
        try:
            async with database() as session:
                if role == "old":
                    return await blog.BlogSubscriptionService().subscribe(session, {"email": email})
                callback = lambda _session: new_committed.set()
                event.listen(session.sync_session, "after_commit", callback)
                try:
                    personal_token = await session.scalar(select(BlogSubscription.unsubscribe_token).where(BlogSubscription.email == email))
                    return await blog.BlogSubscriptionService().unsubscribe(session, email=email, token=personal_token, reason="After cancellation")
                finally:
                    event.remove(session.sync_session, "after_commit", callback)
        finally:
            ROLE.reset(context)

    old = asyncio.create_task(operation("old"))
    tasks = [old]
    try:
        await bounded(contact.old_started.wait())
        old.cancel()
        tasks.append(asyncio.create_task(operation("new")))
        await bounded(new_committed.wait())
        await trace.prove_new_waits_for_old(tasks[1])
        old.cancel()  # Repeated cancellation still cannot abandon the thread.
        await trace.prove_new_waits_for_old(tasks[1])
        assert not old.done() and contact.active == 1 and not contact.idle.is_set()
        assert not contact.new_finished.is_set()
        newer, events, _ = await local_state(database, email)
        assert newer.status == BlogSubscriptionStatus.unsubscribed
        assert [row.event_type for row in events] == [BlogSubscriptionEventType.subscribed, BlogSubscriptionEventType.unsubscribed]
        contact.release_old.set()
        results = await bounded(asyncio.gather(*tasks, return_exceptions=True))
        assert isinstance(results[0], asyncio.CancelledError)
        assert isinstance(results[1], BlogSubscription) and results[1].id == newer.id
        assert contact.idle.is_set() and contact.blacklisted is True
        done = await sync_job(database, newer.id)
        assert (done.desired_version, done.synced_version) == (2, 2)
        async with database() as observer:
            assert await observer.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid IN (:old, :new)"
            ), {"old": trace.granted["old"], "new": trace.granted["new"]}) == 0
        record_property("brevo_cancellation", json.dumps(dict(
            blocking_edges=trace.edges, attempts=trace.attempts, calls=contact.calls,
            final_synced_version=done.synced_version,
        ), sort_keys=True))
    finally:
        contact.release_old.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        trace.remove()
        assert await asyncio.to_thread(contact.idle.wait, 5)


async def test_two_request_callbacks_use_only_two_pinned_pool_connections(database, monkeypatch, record_property):
    """Both real local commits precede concurrent HTTP, with no pool slot left."""
    emails = {"synthetic-pool-one@example.test", "synthetic-pool-two@example.test"}
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    guard = threading.Lock()
    calls = []
    active = set()

    def request(method, path, payload):
        assert method == "POST" and path == "/contacts" and payload["email"] in emails
        assert payload == {"email": payload["email"], "emailBlacklisted": False, "updateEnabled": True, "listIds": [42]}
        with guard:
            active.add(payload["email"])
            calls.append(deepcopy(payload))
            if active == emails:
                loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(20), "Two-connection provider gate was not released"
            return {}
        finally:
            with guard:
                active.remove(payload["email"])

    async with database() as observer:
        schema = await observer.scalar(text("SELECT current_schema()"))
    engine = create_async_engine(database.kw["bind"].url, pool_size=2, max_overflow=0, pool_timeout=5,
                                 connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    holders = set()

    def after_sql(connection, _cursor, statement, _parameters, _context, _executemany):
        sql = statement.upper()
        if "PG_ADVISORY_LOCK" in sql:
            holders.add(connection.connection.driver_connection.get_server_pid())

    event.listen(engine.sync_engine, "after_cursor_execute", after_sql)
    monkeypatch.setattr(settings, "brevo_sync_enabled", True)
    monkeypatch.setattr(settings, "brevo_api_key", "synthetic-pool-key")
    monkeypatch.setattr(settings, "brevo_contact_list_id", 42)
    sync = BrevoContactSyncService(session_factory=sessions)
    monkeypatch.setattr(sync, "_request_json", request)
    monkeypatch.setattr(blog, "brevo_contact_sync_service", sync)

    async def subscribe(email):
        async with sessions() as session:
            return await blog.BlogSubscriptionService().subscribe(session, {"email": email})

    tasks = [asyncio.create_task(subscribe(email)) for email in sorted(emails)]
    try:
        await bounded(started.wait())
        assert all(not task.done() for task in tasks)
        assert len(holders) == 2
        first_pid, second_pid = sorted(holders)
        async with database() as observer:
            rows = (await observer.execute(text(
                "SELECT s.email, j.desired_version, j.synced_version, j.attempt_version "
                "FROM blog_subscriptions s JOIN blog_brevo_sync_jobs j ON j.subscription_id=s.id"
            ))).all()
            assert {row.email for row in rows} == emails
            assert all((row.desired_version, row.synced_version, row.attempt_version) == (1, 0, 1) for row in rows)
            locks = (await observer.execute(text(
                "SELECT l.pid, a.state, a.xact_start FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                "WHERE l.locktype='advisory' AND l.granted AND l.pid IN (:first, :second)"
            ), {"first": first_pid, "second": second_pid})).all()
            assert {row.pid for row in locks} == holders
            assert await observer.scalar(text("SELECT pg_backend_pid()")) not in holders
            assert all(row.state == "idle" and row.xact_start is None for row in locks)
            record_property("brevo_two_connection_pool", json.dumps(dict(
                holder_pids=sorted({row.pid for row in locks}), calls=calls,
                pool_size=2, max_overflow=0,
            ), sort_keys=True))
        release.set()
        results = await bounded(asyncio.gather(*tasks))
        assert {row.email for row in results} == emails and len(calls) == 2
        async with sessions() as session:
            assert await session.scalar(text("SELECT 1")) == 1
            rows = list(await session.scalars(select(BlogBrevoSyncJob)))
            assert len(rows) == 2 and all(row.desired_version == row.synced_version == 1 for row in rows)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(engine.sync_engine, "after_cursor_execute", after_sql)
        await engine.dispose()
