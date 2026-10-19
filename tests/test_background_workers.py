"""Inert workers and fake session IO: never start application schedulers."""
import asyncio
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from app.core.background_workers import BackgroundWorkers, OWNER_KEY, PostgresOwnerLease


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one(self):
        return self.value

    def scalar_one(self):
        return self.value


class Connection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, acquired=True):
        self.acquired = acquired
        self.closed = False
        self.invalidated = False
        self.events = []
        self.healthy = True
        self.failure = None
        self.block_execute = None

    async def execute(self, sql, params):
        statement = str(sql)
        self.events.append(("execute", statement, params))
        if self.block_execute:
            await self.block_execute.wait()
        if self.failure:
            raise self.failure
        if "pg_try_advisory_lock" in statement:
            return Result({"acquired": self.acquired, "backend_pid": 321})
        return Result(self.healthy)

    async def commit(self):
        self.events.append(("commit",))

    async def close(self):
        self.events.append(("close",))
        self.closed = True

    async def invalidate(self):
        self.events.append(("invalidate",))
        self.invalidated = True

    async def connect(self):
        self.events.append(("connect",))
        return self


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


def inert(events, name="job", *, drain_gate=None):
    async def worker():
        events.append(("start", name))
        try:
            await asyncio.Event().wait()
        finally:
            events.append(("drain", name))
            if drain_gate:
                await drain_gate.wait()
            events.append(("done", name))
    return worker


@pytest.mark.anyio
async def test_lease_uses_one_session_and_non_reentrant_heartbeat():
    connection = Connection()
    lease = PostgresOwnerLease(connection.connect)
    assert await lease.acquire()
    assert await lease.heartbeat()
    await lease.release()
    sqls = [event[1] for event in connection.events if event[0] == "execute"]
    assert sum("pg_try_advisory_lock" in sql for sql in sqls) == 1
    assert "pg_locks" in sqls[1] and "objsubid = 2" in sqls[1]
    assert "pg_backend_pid() = :backend_pid" in sqls[1]
    assert "pg_advisory_unlock" in sqls[-1]
    assert connection.events[-1] == ("close",)
    assert not connection.invalidated
    assert all(event[2]["class_key"] == OWNER_KEY[0] for event in connection.events if event[0] == "execute")


@pytest.mark.anyio
@pytest.mark.parametrize("attribute", ["closed", "invalidated"])
async def test_heartbeat_never_reconnects_lost_session(attribute):
    connection = Connection()
    lease = PostgresOwnerLease(connection.connect)
    await lease.acquire()
    setattr(connection, attribute, True)
    before = len(connection.events)
    assert not await lease.heartbeat()
    assert len(connection.events) == before
    await lease.release()
    assert connection.invalidated


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [RuntimeError("secret synthetic DSN"), asyncio.CancelledError()])
async def test_uncertain_acquire_discards_physical_session(failure):
    connection = Connection()
    connection.failure = failure
    lease = PostgresOwnerLease(connection.connect)
    with pytest.raises(type(failure)):
        await lease.acquire()
    assert connection.invalidated and connection.closed
    assert lease.connection is None


@pytest.mark.anyio
async def test_acquire_timeout_discards_session():
    connection = Connection()
    connection.block_execute = asyncio.Event()
    lease = PostgresOwnerLease(connection.connect, timeout_seconds=.01)
    with pytest.raises(TimeoutError):
        await lease.acquire()
    assert connection.invalidated and connection.closed


@pytest.mark.anyio
async def test_contended_follower_starts_nothing_and_never_promotes():
    connection = Connection(acquired=False)
    invoked = []
    workers = BackgroundWorkers([("inert", lambda: invoked.append(True))], app_env="production",
                                connection_factory=connection.connect, heartbeat_seconds=.001)
    async with workers:
        assert workers.state == {"name": "background-workers", "role": "follower", "state": "standby",
                                 "last_error_code": "owner_busy", "scheduler_names": []}
        connection.acquired = True
        await asyncio.sleep(.02)
        assert invoked == []
        assert sum(event[0] == "connect" for event in connection.events) == 1
    assert workers.state["state"] == "standby"


@pytest.mark.anyio
async def test_database_unavailable_is_api_only_and_sanitized(caplog):
    async def unavailable():
        raise RuntimeError("postgresql://secret-password@example.invalid")
    invoked = []
    workers = BackgroundWorkers([("inert", lambda: invoked.append(True))], app_env="staging",
                                connection_factory=unavailable)
    async with workers:
        assert workers.state["state"] == "failed"
        assert workers.state["last_error_code"] == "owner_unavailable"
        assert invoked == []
    assert "secret-password" not in str(workers.state) + caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("app_env", [None, "local", "development", "test"])
async def test_local_lifecycle_bypasses_database_and_preserves_names(app_env):
    async def forbidden():
        pytest.fail("local must not connect")
    events = []
    workers = BackgroundWorkers([("first", inert(events, "first")), ("second", inert(events, "second"))],
                                app_env=app_env, connection_factory=forbidden)
    async with workers:
        await eventually(lambda: len(events) == 2)
        assert workers.state["role"] == "local"
        assert workers.state["scheduler_names"] == ["first", "second"]
    assert workers.state["state"] == "stopped"
    assert {name for kind, name in events if kind == "done"} == {"first", "second"}


@pytest.mark.anyio
async def test_owner_loss_cancels_all_and_drains_before_unlock():
    connection = Connection()
    events, drain_gate = [], asyncio.Event()
    workers = BackgroundWorkers([("one", inert(events, "one", drain_gate=drain_gate)),
                                 ("two", inert(events, "two"))], require_ownership=True,
                                connection_factory=connection.connect, heartbeat_seconds=.005)
    await workers.start()
    await eventually(lambda: len(events) == 2)
    connection.healthy = False
    await eventually(lambda: workers.state["state"] == "lost")
    await eventually(lambda: ("done", "two") in events)
    assert ("drain", "one") in events
    assert not connection.closed
    drain_gate.set()
    await workers.stop()
    assert connection.closed
    assert workers.state["state"] == "lost"
    assert workers.state["last_error_code"] == "owner_lost"
    assert workers.state["scheduler_names"] == ["one", "two"]
    assert sum(event[0] == "connect" for event in connection.events) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [False, True])
async def test_unexpected_worker_exit_fences_other_workers(failure):
    events = []
    exit_gate = asyncio.Event()
    async def exits():
        await exit_gate.wait()
        if failure:
            raise RuntimeError("synthetic failure")
    workers = BackgroundWorkers([("exit", exits), ("other", inert(events))], heartbeat_seconds=.005)
    await workers.start()
    await eventually(lambda: bool(events))
    exit_gate.set()
    await eventually(lambda: workers.state["state"] == "failed")
    with (pytest.raises(RuntimeError) if failure else nullcontext()):
        await workers.stop()
    assert ("done", "job") in events
    assert workers.state["last_error_code"] == ("worker_failed" if failure else "worker_exited")


@pytest.mark.anyio
async def test_normal_shutdown_holds_lease_through_repeated_cancellation():
    connection = Connection()
    events, drain_gate = [], asyncio.Event()
    workers = BackgroundWorkers([("one", inert(events, drain_gate=drain_gate))], require_ownership=True,
                                connection_factory=connection.connect, heartbeat_seconds=.005)
    await workers.start()
    await eventually(lambda: bool(events))
    stopping = asyncio.create_task(workers.stop())
    await eventually(lambda: ("drain", "job") in events)
    stopping.cancel()
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not connection.closed and not stopping.done()
    drain_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert connection.closed and ("done", "job") in events
    assert workers.state["state"] == "stopped"


@pytest.mark.anyio
async def test_startup_failure_drains_partial_workers_and_keeps_primary_error():
    connection = Connection()
    events = []
    error = ValueError("startup")
    def failed():
        raise error
    workers = BackgroundWorkers([("first", inert(events)), ("failed", failed)], require_ownership=True,
                                connection_factory=connection.connect)
    with pytest.raises(ValueError) as observed:
        await workers.start()
    assert observed.value is error
    assert all(task.done() for task in workers.tasks)
    assert connection.closed
    assert workers.state["last_error_code"] == "startup_failed"


@pytest.mark.anyio
async def test_no_schedulers_means_no_connection():
    async def forbidden():
        pytest.fail("empty worker set must not connect")
    async with BackgroundWorkers([], app_env="production", connection_factory=forbidden) as workers:
        assert workers.state["state"] == "disabled"


@pytest.fixture
def owner_pg_url():
    """Opt-in only: the rehearsal creates a new empty UUID database, never a source."""
    import os
    import re
    from sqlalchemy.engine import make_url

    raw = os.environ.get("BACKGROUND_WORKERS_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("isolated PostgreSQL owner rehearsal not requested")
    url = make_url(raw)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "127.0.0.1" and url.port is not None and 1024 <= url.port <= 65535
    assert re.fullmatch(r"sc_shop_r6_owner_test_[a-f0-9]{32}", url.database or "")
    return url


@pytest.mark.anyio
async def test_actual_pg_single_owner_no_automatic_follower_promotion(owner_pg_url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(owner_pg_url, poolclass=NullPool)
    events = []
    first = BackgroundWorkers([("first", inert(events, "first"))], app_env="production",
                              connection_factory=engine.connect, heartbeat_seconds=.01)
    second = BackgroundWorkers([("second", inert(events, "second"))], app_env="production",
                               connection_factory=engine.connect, heartbeat_seconds=.01)
    third = BackgroundWorkers([("third", inert(events, "third"))], app_env="production",
                              connection_factory=engine.connect, heartbeat_seconds=.01)
    try:
        async with engine.connect() as check:
            assert await check.scalar(text("SELECT current_database()")) == owner_pg_url.database
        await first.start()
        await eventually(lambda: ("start", "first") in events)
        await second.start()
        assert first.state["role"] == "owner" and first.state["state"] == "running"
        assert second.state["role"] == "follower" and second.tasks == []
        pid = first.lease.backend_pid
        # Lock survives heartbeat commits and the same backend remains authoritative.
        await asyncio.sleep(.04)
        assert first.state["state"] == "running"
        assert first.lease.backend_pid == pid
        await first.stop()
        await asyncio.sleep(.04)
        assert second.state["state"] == "standby" and ("start", "second") not in events
        await third.start()
        await eventually(lambda: ("start", "third") in events)
        assert third.state["role"] == "owner"
    finally:
        await first.stop()
        await second.stop()
        await third.stop()
        await engine.dispose()


@pytest.mark.anyio
async def test_actual_pg_backend_loss_is_terminal_and_drained(owner_pg_url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(owner_pg_url, poolclass=NullPool)
    events = []
    workers = BackgroundWorkers([("inert", inert(events))], require_ownership=True,
                                connection_factory=engine.connect, heartbeat_seconds=.01)
    follower = BackgroundWorkers([("follower", inert(events, "follower"))], require_ownership=True,
                                 connection_factory=engine.connect, heartbeat_seconds=.01)
    try:
        await workers.start()
        await eventually(lambda: bool(events))
        await follower.start()
        pid = workers.lease.backend_pid
        async with engine.connect() as admin:
            # Only our held session in this new UUID database is eligible.
            terminated = await admin.scalar(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database AND pid = :pid AND pid <> pg_backend_pid()"),
                {"database": owner_pg_url.database, "pid": pid})
            assert terminated is True
        await eventually(lambda: workers.state["state"] == "lost")
        await eventually(lambda: ("done", "job") in events)
        await workers.stop()
        await asyncio.sleep(.04)
        assert follower.state["state"] == "standby" and follower.tasks == []
        assert workers.lease.connection is None
        assert workers.state["last_error_code"] == "owner_lost"
    finally:
        await workers.stop()
        await follower.stop()
        await engine.dispose()


@pytest.mark.anyio
async def test_shutdown_does_not_cancel_inflight_owner_heartbeat_before_worker_drain():
    connection = Connection()
    events, drain_gate = [], asyncio.Event()
    workers = BackgroundWorkers([("one", inert(events, drain_gate=drain_gate))], require_ownership=True,
                                connection_factory=connection.connect, heartbeat_seconds=.005)
    await workers.start()
    await eventually(lambda: bool(events))
    connection.block_execute = asyncio.Event()
    await eventually(lambda: any(event[0] == "execute" and "pg_locks" in event[1]
                                 for event in connection.events))
    stopping = asyncio.create_task(workers.stop())
    await asyncio.sleep(.01)
    assert not connection.closed and not connection.invalidated
    assert ("drain", "job") not in events
    connection.block_execute.set()
    await eventually(lambda: ("drain", "job") in events)
    assert not connection.closed and not connection.invalidated
    drain_gate.set()
    await stopping
    assert connection.closed and not connection.invalidated
    assert events[-1] == ("done", "job")
