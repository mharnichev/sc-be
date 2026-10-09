"""Actual ORM master cascade versus availability deletion on isolated PostgreSQL.

The observer proves backend lock edges and records server SQLSTATEs. Engine
events gate real flush SQL, including DELETEs invisible to AsyncSession.execute
wrappers. No bookings/uploads/providers are involved in these two cases.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.util.concurrency import await_only

from app.api.v1.routes.bookings import admin_delete_master
from app.models.booking import Booking, Master, MasterAvailabilityWindow
from app.services.booking import BookingServiceLayer, KYIV_TZ
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


async def seed(database):
    day = datetime.now(KYIV_TZ).date() + timedelta(days=2)
    while day.weekday() == 0:
        day += timedelta(days=1)
    start = datetime(day.year, day.month, day.day, 10, tzinfo=KYIV_TZ)
    async with database() as session:
        master = Master(full_name="Synthetic cascade master", is_active=True)
        session.add(master)
        await session.flush()
        window = MasterAvailabilityWindow(master_id=master.id, start_at=start, end_at=start + timedelta(hours=1))
        session.add(window)
        await session.commit()
        assert await session.scalar(select(Booking.id).limit(1)) is None
        assert master.photo_upload_id is master.avatar_upload_id is master.passport_photo_upload_id is None
        return master.id, window.id


class MasterCascadeRace:
    def __init__(self, database):
        self.database = database
        self.pids = {}
        self.ready = {role: asyncio.Event() for role in ("admin", "window")}
        self.go = {role: asyncio.Event() for role in ("admin", "window")}
        self.admin_stage = asyncio.Event()
        self.window_stage = asyncio.Event()
        self.admin_mode_known = asyncio.Event()
        self.release_admin = asyncio.Event()
        self.release_window = asyncio.Event()
        self.admin_parent_first = None
        self.edges = []
        self.sql = []

    @staticmethod
    def statement_kind(statement):
        sql = " ".join(statement.replace('"', '').upper().split())
        if sql.startswith("DELETE FROM MASTER_AVAILABILITY_WINDOWS "):
            return "child-delete"
        if sql.startswith("DELETE FROM MASTERS "):
            return "parent-delete"
        if sql.startswith("SELECT ") and "FROM MASTERS " in sql and "FOR UPDATE" in sql:
            return "parent-lock"
        return None

    def before_sql(self, connection, cursor, statement, parameters, context, executemany):
        role = connection.info.get("master_cascade_role")
        if role == "admin" and self.statement_kind(statement) == "parent-lock":
            self.admin_parent_first = True
            self.admin_mode_known.set()

    def after_sql(self, connection, cursor, statement, parameters, context, executemany):
        role = connection.info.get("master_cascade_role")
        kind = self.statement_kind(statement)
        if role not in self.ready or kind is None:
            return
        self.sql.append(dict(role=role, kind=kind, rowcount=cursor.rowcount))
        if role == "admin" and kind == "parent-lock":
            self.admin_stage.set()
            await_only(asyncio.wait_for(self.release_admin.wait(), timeout=10))
        elif role == "admin" and kind == "child-delete":
            assert cursor.rowcount == 1, "Cascade gate must follow a real child-row deletion"
            if self.admin_parent_first is None:
                self.admin_parent_first = False
                self.admin_mode_known.set()
            self.admin_stage.set()
            await_only(asyncio.wait_for(self.release_admin.wait(), timeout=10))
        elif role == "window" and kind == "parent-lock":
            self.window_stage.set()
            await_only(asyncio.wait_for(self.release_window.wait(), timeout=10))

    async def prepare(self, session, role, master_id, window_id):
        await session.execute(text("SET LOCAL lock_timeout = '10s'"))
        await session.execute(text("SET LOCAL statement_timeout = '15s'"))
        self.pids[role] = await session.scalar(text("SELECT pg_backend_pid()"))
        assert len(set(self.pids.values())) == len(self.pids)
        connection = await session.connection()
        connection.sync_connection.info["master_cascade_role"] = role
        master = await session.get(Master, master_id)
        window = await session.get(MasterAvailabilityWindow, window_id)
        assert master is not None and window is not None
        assert master.is_active and window.master_id == master.id
        self.ready[role].set()
        await asyncio.wait_for(self.go[role].wait(), timeout=10)
        return window

    async def observe(self, waiter_role, holder_role, *, tasks, cycle=False):
        waiter, holder = self.pids[waiter_role], self.pids[holder_role]
        assert waiter != holder
        async with self.database() as observer:
            assert await observer.scalar(text("SELECT pg_backend_pid()")) not in self.pids.values()
            async with asyncio.timeout(5):
                while True:
                    rows = (await observer.execute(text(
                        "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                        "WHERE pid IN (:admin, :window)"
                    ), self.pids)).all()
                    blockers = {row.pid: row.blockers for row in rows}
                    for role in ("admin", "window"):
                        other = "window" if role == "admin" else "admin"
                        edge = [self.pids[role], self.pids[other]]
                        if edge[1] in blockers.get(edge[0], []) and edge not in self.edges:
                            self.edges.append(edge)
                    if holder in blockers.get(waiter, []):
                        if not cycle or waiter in blockers.get(holder, []):
                            return
                    # An unusually short server deadlock_timeout may resolve
                    # the cycle before the next sample. The captured actual
                    # 40P01 below remains mandatory for any failed operation.
                    if cycle and any(task.done() for task in tasks):
                        return
                    await asyncio.sleep(0.01)


@pytest.mark.anyio
@pytest.mark.parametrize("first", ["admin", "window"], ids=["cascade-first", "availability-first"])
async def test_master_cascade_and_availability_delete_finish_without_inverse_lock_deadlock(database, first, record_property):
    master_id, window_id = await seed(database)
    race = MasterCascadeRace(database)
    engine = database.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", race.before_sql)
    event.listen(engine, "after_cursor_execute", race.after_sql)

    async def operation(role):
        async with database() as session:
            window = await race.prepare(session, role, master_id, window_id)
            try:
                if role == "admin":
                    await admin_delete_master(master_id, current_user=SimpleNamespace(is_superuser=True), session=session)
                else:
                    await BookingServiceLayer().delete_availability_window(session, window, allow_booked=False)
                return dict(role=role, status=204)
            except DBAPIError as error:
                await session.rollback()
                return dict(role=role, sqlstate=getattr(error.orig, "sqlstate", None)
                            or getattr(error.orig, "pgcode", None), database_error=str(error))

    tasks = {role: asyncio.create_task(operation(role)) for role in ("admin", "window")}
    try:
        await asyncio.wait_for(asyncio.gather(*(ready.wait() for ready in race.ready.values())), timeout=5)
        assert len(set(race.pids.values())) == 2
        race.go[first].set()
        if first == "admin":
            await asyncio.wait_for(race.admin_stage.wait(), timeout=5)
            race.go["window"].set()
        else:
            await asyncio.wait_for(race.window_stage.wait(), timeout=5)
            race.go["admin"].set()
        await asyncio.wait_for(race.admin_mode_known.wait(), timeout=5)
        if race.admin_parent_first:
            # Ordered route: whichever writer starts second must wait on the
            # first real parent lock; release without manufacturing a cycle.
            second = "window" if first == "admin" else "admin"
            await race.observe(second, first, tasks=list(tasks.values()))
            if first == "admin":
                race.release_admin.set()
                race.release_window.set()
            else:
                race.release_window.set()
                await asyncio.wait_for(race.admin_stage.wait(), timeout=5)
                race.release_admin.set()
        else:
            # Current route owns the real deleted child row; availability owns
            # the parent. First prove the child wait, then let admin reach its
            # parent DELETE and capture the actual reciprocal PostgreSQL wait.
            await asyncio.wait_for(race.admin_stage.wait(), timeout=5)
            await asyncio.wait_for(race.window_stage.wait(), timeout=5)
            assert any(item["role"] == "admin" and item["kind"] == "child-delete" and item["rowcount"] == 1
                       for item in race.sql)
            race.release_window.set()
            await race.observe("window", "admin", tasks=list(tasks.values()))
            assert [race.pids["window"], race.pids["admin"]] in race.edges
            race.release_admin.set()
            await race.observe("admin", "window", tasks=list(tasks.values()), cycle=True)
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks.values()), timeout=20)
        async with database() as verify:
            masters = list((await verify.scalars(select(Master))).all())
            windows = list((await verify.scalars(select(MasterAvailabilityWindow))).all())
            bookings = list((await verify.scalars(select(Booking))).all())
        diagnostics = dict(first=first, pids=race.pids, parent_first=race.admin_parent_first,
                           blocking_edges=race.edges, sql=race.sql, outcomes=outcomes,
                           persisted=dict(masters=len(masters), windows=len(windows), bookings=len(bookings)))
        record_property("master_cascade_race", json.dumps(diagnostics, sort_keys=True))
        errors = [item for item in outcomes if "database_error" in item]
        assert all(item["sqlstate"] == "40P01" for item in errors), diagnostics
        assert not errors, f"Real PostgreSQL inverse cascade/availability lock deadlock: {diagnostics}"
        assert outcomes == [dict(role="admin", status=204), dict(role="window", status=204)]
        assert masters == windows == bookings == []
    finally:
        for gate in [*race.go.values(), race.release_admin, race.release_window]:
            gate.set()
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        event.remove(engine, "before_cursor_execute", race.before_sql)
        event.remove(engine, "after_cursor_execute", race.after_sql)
