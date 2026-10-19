"""Process-local worker lifecycle with a dedicated PostgreSQL session owner lease.

A follower never promotes itself. The lease fences scheduler admission, not external
exactly-once delivery: existing durable send/queue fences remain authoritative.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)
OWNER_KEY = (1396919372, 1)  # Stable application namespace, session advisory lock.


def worker_state(role: str = "disabled", state: str = "disabled") -> dict[str, Any]:
    return {"name": "background-workers", "role": role, "state": state,
            "last_error_code": None, "scheduler_names": []}


async def _drain(task):
    """Finish cleanup even if its caller receives repeated cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class PostgresOwnerLease:
    def __init__(self, connection_factory=None, *, timeout_seconds: float = 5):
        self.connection_factory = connection_factory
        self.timeout_seconds = timeout_seconds
        self.connection = None
        self.backend_pid = None
        self.owned = False

    async def _bounded(self, awaitable):
        return await asyncio.wait_for(awaitable, self.timeout_seconds)

    async def acquire(self) -> bool:
        try:
            if self.connection_factory is None:
                from app.core.database import engine
                factory = engine.connect
            else:
                factory = self.connection_factory
            self.connection = await self._bounded(factory())
            if self.connection.dialect.name != "postgresql":
                raise RuntimeError("owner_database_unsupported")
            result = await self._bounded(self.connection.execute(text(
                "SELECT pg_try_advisory_lock(:class_key, :object_key) AS acquired, "
                "pg_backend_pid() AS backend_pid"),
                {"class_key": OWNER_KEY[0], "object_key": OWNER_KEY[1]}))
            row = result.mappings().one()
            self.backend_pid = row["backend_pid"]
            self.owned = bool(row["acquired"])
            await self._bounded(self.connection.commit())
            if not self.owned:
                await self._bounded(self.connection.close())
                self.connection = None
            return self.owned
        except BaseException:
            await _drain(asyncio.create_task(self._discard()))
            raise

    async def heartbeat(self) -> bool:
        connection = self.connection
        # Never let SQLAlchemy reconnect a lost session and appear to own its lock.
        if connection is None or connection.closed or connection.invalidated or not self.owned:
            return False
        result = await self._bounded(connection.execute(text(
            "SELECT pg_backend_pid() = :backend_pid AND EXISTS ("
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid = pg_backend_pid() AND classid = :class_key "
            "AND objid = :object_key AND objsubid = 2 "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))"),
            {"backend_pid": self.backend_pid, "class_key": OWNER_KEY[0], "object_key": OWNER_KEY[1]}))
        healthy = result.scalar_one() is True
        await self._bounded(connection.commit())
        return healthy

    async def _discard(self):
        connection, self.connection = self.connection, None
        self.owned = False
        if connection is not None:
            # Invalidation closes the physical session, including an uncertain lock.
            try:
                await self._bounded(connection.invalidate())
            finally:
                await self._bounded(connection.close())

    async def release(self, *, lost=False):
        if lost:
            await self._discard()
            return
        connection = self.connection
        if connection is None:
            return
        try:
            if connection.closed or connection.invalidated:
                await self._discard()
                return
            if self.owned:
                result = await self._bounded(connection.execute(text(
                    "SELECT pg_advisory_unlock(:class_key, :object_key)"),
                    {"class_key": OWNER_KEY[0], "object_key": OWNER_KEY[1]}))
                if result.scalar_one() is not True:
                    raise RuntimeError("owner_unlock_failed")
                await self._bounded(connection.commit())
            await self._bounded(connection.close())
            self.connection = None
            self.owned = False
        except BaseException:
            await self._discard()
            raise


class BackgroundWorkers:
    def __init__(self, schedulers: Sequence[tuple[str, Callable]], *, app_env=None,
                 require_ownership=None, connection_factory=None, create_task=None,
                 heartbeat_seconds: float = 5):
        self.schedulers = schedulers
        self.require_ownership = (app_env in {"production", "staging"}
                                  if require_ownership is None else require_ownership)
        self.lease = PostgresOwnerLease(connection_factory)
        self.create_task = create_task or asyncio.create_task
        self.heartbeat_seconds = heartbeat_seconds
        self.state = worker_state("owner" if self.require_ownership else "local", "starting")
        self.tasks = []
        self.monitor = None
        self.cleanup = None
        self.stop_task = None

    def _status(self, state, error=None):
        self.state.update(state=state, last_error_code=error)
        logger.info("background_workers state=%s role=%s code=%s", state, self.state["role"], error)

    async def start(self):
        if not self.schedulers:
            self.state.update(role="disabled", state="disabled")
            return
        if self.require_ownership:
            try:
                owned = await self.lease.acquire()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._status("failed", "owner_unavailable")
                return
            if not owned:
                self.state["role"] = "follower"
                self._status("standby", "owner_busy")
                return
        try:
            for name, factory in self.schedulers:
                coroutine = factory()
                try:
                    task = self.create_task(coroutine, name=name)
                except BaseException:
                    if inspect.iscoroutine(coroutine):
                        coroutine.close()
                    raise
                self.tasks.append(task)
                self.state["scheduler_names"].append(name)
        except BaseException:
            self._status("failed", "startup_failed")
            with suppress(BaseException):
                await _drain(self._cleanup_task())
            raise
        self._status("running")
        self.monitor = asyncio.create_task(self._monitor(), name="background-workers-supervisor")

    async def _monitor(self):
        try:
            while True:
                done, _ = await asyncio.wait(self.tasks, timeout=self.heartbeat_seconds,
                                             return_when=asyncio.FIRST_COMPLETED)
                if done:
                    failed = any(task.cancelled() or task.exception() is not None for task in done)
                    self._status("failed", "worker_failed" if failed else "worker_exited")
                    break
                if self.require_ownership:
                    try:
                        heartbeat = asyncio.create_task(self.lease.heartbeat(), name="background-owner-heartbeat")
                        try:
                            healthy = await asyncio.shield(heartbeat)
                        except asyncio.CancelledError:
                            # A cancelled SQL query may invalidate the owning session.
                            # Finish this bounded query before normal shutdown drains
                            # workers and releases the lease.
                            with suppress(Exception):
                                await _drain(heartbeat)
                            raise
                    except Exception:
                        healthy = False
                    if not healthy:
                        self._status("lost", "owner_lost")
                        break
            # Cleanup owns cancellation/drain and lease release independently of monitor.
            with suppress(Exception):
                await asyncio.shield(self._cleanup_task())
        except asyncio.CancelledError:
            raise

    def _cleanup_task(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._cleanup(), name="background-workers-cleanup")
        return self.cleanup

    async def _cleanup(self):
        first_error = None
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            await self.lease.release(lost=self.state["state"] == "lost")
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            if self.state["state"] not in {"lost", "failed"}:
                self._status("failed", "shutdown_failed")
            raise first_error
        if self.state["state"] not in {"lost", "failed", "disabled", "standby"}:
            self._status("stopped")

    async def _stop(self):
        try:
            if self.monitor is not None:
                self.monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await self.monitor
        finally:
            if self.state["state"] == "running":
                self._status("stopping")
            await asyncio.shield(self._cleanup_task())

    async def stop(self):
        if self.stop_task is None:
            self.stop_task = asyncio.create_task(self._stop(), name="background-workers-stop")
        await _drain(self.stop_task)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()
