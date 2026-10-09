"""Lose acknowledgements only after real PostgreSQL lock operations complete."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import settings
from app.services import blog
from tests.test_blog_brevo_sync_integration import SyntheticContact, install_provider, sync_job
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


pytestmark = pytest.mark.anyio


async def backend_state(database, pid):
    """Observe through an unwrapped connection, independently of the owner."""
    async with database() as observer:
        row = (await observer.execute(text(
            "SELECT pg_backend_pid() AS observer_pid, "
            "(SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
            "AND granted AND pid=:pid) AS granted_locks, "
            "(SELECT count(*) FROM pg_stat_activity WHERE pid=:pid) AS backends"
        ), {"pid": pid})).one()
        assert row.observer_pid != pid
        return dict(row._mapping)


@dataclass
class AcknowledgementFaults:
    acquire_error: BaseException | None = None
    unlock_error: BaseException | None = None
    invalidate_error: BaseException | None = None
    acquired_fault: bool = False
    unlocked_fault: bool = False
    invalidated_fault: bool = False
    owner_pid: int | None = None
    invalidations: int = 0
    observations: list[tuple[str, dict]] = field(default_factory=list)


class FaultedCoordinatorConnection(AsyncConnection):
    """Keep SQLAlchemy's real connection; inject only at coordinator awaits."""

    def __init__(self, engine, database, faults):
        super().__init__(engine)
        self.observer_database = database
        self.faults = faults

    def backend_pid(self):
        return self.sync_connection.connection.driver_connection.get_server_pid()

    async def execute(self, statement, *args, **kwargs):
        result = await super().execute(statement, *args, **kwargs)
        if ("pg_advisory_lock(" in str(statement).lower()
                and self.faults.acquire_error is not None and not self.faults.acquired_fault):
            self.faults.owner_pid = self.backend_pid()
            observed = await backend_state(self.observer_database, self.faults.owner_pid)
            assert observed["granted_locks"] == 1 and observed["backends"] == 1
            self.faults.observations.append(("acquired_before_ack_loss", observed))
            self.faults.acquired_fault = True
            raise self.faults.acquire_error
        return result

    async def scalar(self, statement, *args, **kwargs):
        result = await super().scalar(statement, *args, **kwargs)
        if ("pg_advisory_unlock(" in str(statement).lower()
                and self.faults.unlock_error is not None and not self.faults.unlocked_fault):
            assert result is True, "The real backend must have owned and released the lock"
            self.faults.owner_pid = self.backend_pid()
            observed = await backend_state(self.observer_database, self.faults.owner_pid)
            assert observed["granted_locks"] == 0 and observed["backends"] == 1
            self.faults.observations.append(("unlocked_before_ack_loss", observed))
            self.faults.unlocked_fault = True
            raise self.faults.unlock_error
        return result

    async def invalidate(self, exception=None):
        await super().invalidate(exception)
        assert self.invalidated
        self.faults.invalidations += 1
        if self.faults.invalidate_error is not None and not self.faults.invalidated_fault:
            self.faults.invalidated_fault = True
            raise self.faults.invalidate_error


class CoordinatorEngine:
    def __init__(self, database, faults):
        self.database = database
        self.faults = faults

    def connect(self):
        return FaultedCoordinatorConnection(self.database.kw["bind"], self.database, self.faults)


class CoordinatorSessionFactory:
    """Only the coordinator's explicit connect uses the faulted connection."""

    def __init__(self, database, faults):
        self.database = database
        self.kw = {**database.kw, "bind": CoordinatorEngine(database, faults)}

    def __call__(self, *args, **kwargs):
        return self.database(*args, **kwargs)


async def prepare_pending_subscription(database, monkeypatch, faults, email):
    contact = SyntheticContact(email)
    sync = install_provider(database, monkeypatch, contact)
    monkeypatch.setattr(settings, "brevo_sync_enabled", False)
    async with database() as session:
        subscription = await blog.BlogSubscriptionService().subscribe(session, {"email": email})
        subscription_id = subscription.id
    monkeypatch.setattr(settings, "brevo_sync_enabled", True)
    sync.session_factory = CoordinatorSessionFactory(database, faults)
    assert contact.calls == []
    return subscription_id, sync, contact


async def assert_backend_invalidated_and_pool_reusable(database, faults):
    assert faults.owner_pid is not None and faults.invalidations >= 1
    async with asyncio.timeout(5):
        while True:
            observed = await backend_state(database, faults.owner_pid)
            if observed["granted_locks"] == observed["backends"] == 0:
                break
            await asyncio.sleep(0.01)
    # The terminated backend cannot retain ownership or return to the pool.
    assert database.kw["bind"].pool.checkedout() == 0
    async with database() as session:
        assert await session.scalar(text("SELECT 1")) == 1
        assert await session.scalar(text("SELECT pg_backend_pid()")) != faults.owner_pid
    assert database.kw["bind"].pool.checkedout() == 0


@pytest.mark.parametrize("failure", ["oserror", "cancelled", "cancelled_and_invalidate_ack_loss"])
async def test_lost_acquisition_ack_invalidates_real_owner_and_preserves_pending_intent(
    database, monkeypatch, failure,
):
    primary_error = (OSError("Synthetic acquisition acknowledgement loss") if failure == "oserror"
                     else asyncio.CancelledError("Synthetic acquisition cancellation"))
    faults = AcknowledgementFaults(
        acquire_error=primary_error,
        invalidate_error=(OSError("Synthetic invalidation acknowledgement loss")
                          if failure == "cancelled_and_invalidate_ack_loss" else None),
    )
    subscription_id, sync, contact = await prepare_pending_subscription(
        database, monkeypatch, faults, f"synthetic-acquire-{failure.replace('_', '-')}@example.test",
    )

    with pytest.raises(type(primary_error)) as caught:
        async with asyncio.timeout(10):
            await sync._sync_subscription(subscription_id, wait_for_lock=True)
    assert caught.value is primary_error
    assert faults.acquired_fault and [stage for stage, _ in faults.observations] == ["acquired_before_ack_loss"]
    assert contact.calls == []
    pending = await sync_job(database, subscription_id)
    assert (pending.desired_version, pending.synced_version, pending.attempts) == (1, 0, 0)
    assert pending.attempt_version is None and pending.last_attempt_at is None
    if faults.invalidate_error is not None:
        assert faults.invalidated_fault and faults.invalidations == 2
    await assert_backend_invalidated_and_pool_reusable(database, faults)

    # A fresh pinned connection can claim and deliver the original durable intent.
    assert await sync.sync_pending() == 1
    assert await sync.sync_pending() == 0
    done = await sync_job(database, subscription_id)
    assert (done.desired_version, done.synced_version) == (1, 1)
    assert len(contact.calls) == 1 and contact.calls[0]["completed"]
    assert database.kw["bind"].pool.checkedout() == 0


async def test_lost_unlock_ack_invalidates_connection_without_replaying_durable_success(database, monkeypatch):
    unlock_error = OSError("Synthetic unlock acknowledgement loss")
    faults = AcknowledgementFaults(unlock_error=unlock_error)
    subscription_id, sync, contact = await prepare_pending_subscription(
        database, monkeypatch, faults, "synthetic-unlock-ack-loss@example.test",
    )

    with pytest.raises(OSError) as caught:
        async with asyncio.timeout(10):
            await sync._sync_subscription(subscription_id, wait_for_lock=True)
    assert caught.value is unlock_error
    assert faults.unlocked_fault and [stage for stage, _ in faults.observations] == ["unlocked_before_ack_loss"]
    assert len(contact.calls) == 1 and contact.calls[0]["completed"]
    done = await sync_job(database, subscription_id)
    assert (done.desired_version, done.synced_version, done.attempt_version, done.attempts) == (1, 1, 1, 1)
    assert done.last_error is None
    await assert_backend_invalidated_and_pool_reusable(database, faults)
    assert await sync.sync_pending() == 0
    assert len(contact.calls) == 1
