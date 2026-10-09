"""Availability write races on real PostgreSQL, without providers or lifespan.

The shared fixture requires an explicit isolated local SEGMENTS_TEST_DATABASE_URL.
SQL-result barriers reproduce today's unlocked checks. A future physical Master
FOR UPDATE uses observed backend blocking instead, with unchanged outcome checks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.api.v1.routes import bookings as routes
from app.models.booking import BarberService, Booking, BookingStatus, Master, MasterAvailabilityWindow
from app.schemas.booking import MasterAvailabilityDaysCreate, MasterAvailabilityWindowCreate, PublicBookingCreate
from app.services.booking import BookingServiceLayer, KYIV_TZ
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


def next_open_start():
    day = datetime.now(KYIV_TZ).date() + timedelta(days=2)
    while day.weekday() == 0:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, 10, tzinfo=KYIV_TZ)


async def seed(database):
    async with database() as session:
        physical = Master(full_name="Physical calendar", is_active=True, show_on_master_block=False)
        independent = Master(full_name="Independent calendar", is_active=True)
        session.add_all([physical, independent])
        await session.flush()
        aliases = [Master(full_name=f"Public alias {index}", is_active=True,
                          booking_redirect_master_id=physical.id) for index in (1, 2)]
        inactive = Master(full_name="Inactive source alias", is_active=False,
                          booking_redirect_master_id=physical.id)
        session.add_all([*aliases, inactive])
        await session.flush()
        services = {}
        for master in [physical, independent, *aliases]:
            service = BarberService(master_id=master.id, name="Shared synthetic haircut", duration_minutes=30,
                                    price=100, is_active=True)
            session.add(service)
            await session.flush()
            services[master.id] = service.id
        await session.commit()
        return dict(physical=physical.id, independent=independent.id, aliases=[m.id for m in aliases],
                    inactive=inactive.id, services=services, start=next_open_start())


async def bounded_transaction(session):
    await session.execute(text("SET LOCAL lock_timeout = '10s'"))
    await session.execute(text("SET LOCAL statement_timeout = '15s'"))
    return await session.scalar(text("SELECT pg_backend_pid()"))


def selects_entity(statement, entity):
    return any(item.get("entity") is entity for item in getattr(statement, "column_descriptions", []))


def locks_physical_master(statement, physical_id):
    return (getattr(statement, "_for_update_arg", None) is not None
            and selects_entity(statement, Master)
            and physical_id in statement.compile().params.values())


async def wait_for_block_or_completion(database, holder, waiter, task):
    assert holder != waiter
    async with database() as observer:
        assert await observer.scalar(text("SELECT pg_backend_pid()")) not in (holder, waiter)
        async with asyncio.timeout(5):
            while not task.done():
                if await observer.scalar(text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"),
                                         {"holder": holder, "waiter": waiter}):
                    return True
                await asyncio.sleep(0.01)
    return False


class AvailabilityCreateRace:
    def __init__(self, database, physical_id):
        self.database = database
        self.physical_id = physical_id
        self.pids = {}
        self.ready = asyncio.Barrier(2)
        self.unlocked_checks = asyncio.Barrier(2)
        self.mode_known = asyncio.Event()
        self.first_lock_held = asyncio.Event()
        self.release_holder = asyncio.Event()
        self.locked = None
        self.holder = None
        self.lock_attempts = []
        self.completed_overlap_reads = []
        self.blocked_pair = None

    def note_lock_attempt(self, label):
        if self.locked is None:
            self.locked = True
            self.mode_known.set()
        assert self.locked is True, "Master lock occurred after an unlocked overlap check"
        self.lock_attempts.append(label)

    async def after_lock(self, label):
        if self.holder is None:
            self.holder = label
            self.first_lock_held.set()
            await asyncio.wait_for(self.release_holder.wait(), timeout=10)

    async def prepare(self, session, label, monkeypatch):
        self.pids[label] = await bounded_transaction(session)
        assert len(set(self.pids.values())) == len(self.pids)
        execute, get = session.execute, session.get

        async def execute_with_gate(statement, *args, **kwargs):
            locking = locks_physical_master(statement, self.physical_id)
            if locking:
                self.note_lock_attempt(label)
            result = await execute(statement, *args, **kwargs)
            if locking:
                await self.after_lock(label)
            elif selects_entity(statement, MasterAvailabilityWindow) and getattr(statement, "_limit_clause", None) is not None:
                self.completed_overlap_reads.append(label)
                if self.locked is None:
                    self.locked = False
                    self.mode_known.set()
                if not self.locked:
                    await asyncio.wait_for(self.unlocked_checks.wait(), timeout=10)
            return result

        async def get_with_gate(entity, identity, *args, **kwargs):
            locking = entity is Master and identity == self.physical_id and kwargs.get("with_for_update") is not None
            if locking:
                self.note_lock_attempt(label)
            result = await get(entity, identity, *args, **kwargs)
            if locking:
                await self.after_lock(label)
            return result

        monkeypatch.setattr(session, "execute", execute_with_gate)
        monkeypatch.setattr(session, "get", get_with_gate)
        await asyncio.wait_for(self.ready.wait(), timeout=10)

    async def observe(self, tasks):
        await asyncio.wait_for(self.mode_known.wait(), timeout=5)
        if not self.locked:
            return
        await asyncio.wait_for(self.first_lock_held.wait(), timeout=5)
        holder = self.pids[self.holder]
        waiter_label = next(label for label in self.pids if label != self.holder)
        waiter = self.pids[waiter_label]
        blocked = await wait_for_block_or_completion(self.database, holder, waiter, tasks[waiter_label])
        assert blocked, "Second availability writer did not wait on the physical calendar's PostgreSQL lock"
        self.blocked_pair = [holder, waiter]
        self.release_holder.set()

    def assert_interleaving(self):
        assert len(self.pids) == len(set(self.pids.values())) == 2
        if self.locked:
            assert set(self.lock_attempts) == {"left", "right"}
            assert self.blocked_pair is not None
        else:
            assert self.completed_overlap_reads == ["left", "right"] or self.completed_overlap_reads == ["right", "left"]
            assert self.unlocked_checks.n_waiting == 0


async def create_for_master(session, master_id, kind, start, end):
    master = await session.get(Master, master_id)
    if kind == "days":
        result = await routes.create_my_availability_days(MasterAvailabilityDaysCreate(dates=[start.date()]),
                                                          current_master=master, session=session)
        return [item.id for item in result]
    result = await routes.create_my_availability_window(MasterAvailabilityWindowCreate(start_at=start, end_at=end),
                                                        current_master=master, session=session)
    return [result.id]


async def run_create_pair(database, monkeypatch, catalog, *, right_kind, redirected, touching=False):
    race = AvailabilityCreateRace(database, catalog["physical"])
    ids = catalog["aliases"] if redirected else [catalog["physical"], catalog["physical"]]
    start = catalog["start"]

    async def attempt(label, master_id):
        async with database() as session:
            await race.prepare(session, label, monkeypatch)
            offset = timedelta(hours=1) if touching and label == "right" else timedelta(0)
            try:
                rows = await create_for_master(session, master_id, right_kind if label == "right" else "window",
                                               start + offset, start + offset + timedelta(hours=1))
                return dict(status=201, ids=rows)
            except HTTPException as error:
                await session.rollback()
                return dict(status=error.status_code, detail=error.detail)

    tasks = {"left": asyncio.create_task(attempt("left", ids[0])),
             "right": asyncio.create_task(attempt("right", ids[1]))}
    observer = asyncio.create_task(race.observe(tasks))
    try:
        result = await asyncio.wait_for(asyncio.gather(*tasks.values(), observer), timeout=20)
        race.assert_interleaving()
        return result[:2], race
    finally:
        race.release_holder.set()
        all_tasks = [*tasks.values(), observer]
        for task in all_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("right_kind", ["window", "days"])
@pytest.mark.parametrize("redirected", [False, True], ids=["physical", "redirect-aliases"])
async def test_concurrent_overlapping_availability_rejects_one_writer(database, monkeypatch, right_kind, redirected, record_property):
    catalog = await seed(database)
    outcomes, race = await run_create_pair(database, monkeypatch, catalog, right_kind=right_kind, redirected=redirected)
    async with database() as verify:
        rows = list((await verify.scalars(select(MasterAvailabilityWindow))).all())
    record_property("availability_create_race", json.dumps(dict(pids=race.pids, locked=race.locked,
                    blocked_pair=race.blocked_pair, outcomes=outcomes, persisted_ids=[row.id for row in rows])))
    assert sorted(item["status"] for item in outcomes) == [201, 409]
    assert next(item for item in outcomes if item["status"] == 409)["detail"] == "Availability window overlaps an existing window"
    assert len(rows) == 1 and rows[0].master_id == catalog["physical"]
    assert rows[0].start_at <= catalog["start"] < rows[0].end_at


@pytest.mark.anyio
@pytest.mark.parametrize("redirected", [False, True], ids=["physical", "redirect-aliases"])
async def test_concurrent_touching_windows_are_both_allowed(database, monkeypatch, redirected):
    catalog = await seed(database)
    outcomes, _ = await run_create_pair(database, monkeypatch, catalog, right_kind="window", redirected=redirected, touching=True)
    assert [item["status"] for item in outcomes] == [201, 201]
    async with database() as verify:
        rows = list((await verify.scalars(select(MasterAvailabilityWindow).order_by(MasterAvailabilityWindow.start_at))).all())
        assert len(rows) == 2 and {row.master_id for row in rows} == {catalog["physical"]}
        assert rows[0].start_at == catalog["start"] and rows[0].end_at == rows[1].start_at
        assert rows[1].end_at == catalog["start"] + timedelta(hours=2)


@pytest.mark.anyio
@pytest.mark.parametrize("redirected", [False, True], ids=["physical", "redirect-aliases"])
@pytest.mark.parametrize("allow_booked", [False, True], ids=["customer-guard", "admin-override"])
async def test_delete_window_vs_actual_public_booking_preserves_guard_or_explicit_override(
    database, monkeypatch, redirected, allow_booked, record_property,
):
    catalog = await seed(database)
    start = catalog["start"]
    async with database() as setup:
        window = MasterAvailabilityWindow(master_id=catalog["physical"], start_at=start, end_at=start + timedelta(hours=1))
        setup.add(window)
        await setup.commit()
        window_id = window.id
    availability_read = asyncio.Event()
    release_booking = asyncio.Event()
    delete_ready = asyncio.Event()
    pids = {}
    booked_master = catalog["aliases"][0] if redirected else catalog["physical"]
    delete_master = catalog["aliases"][1] if redirected else catalog["physical"]

    async def book():
        async with database() as session:
            pids["booking"] = await bounded_transaction(session)
            execute = session.execute
            physical_locked = False

            async def execute_with_gate(statement, *args, **kwargs):
                nonlocal physical_locked
                result = await execute(statement, *args, **kwargs)
                if locks_physical_master(statement, catalog["physical"]):
                    physical_locked = True
                elif selects_entity(statement, MasterAvailabilityWindow) and not availability_read.is_set():
                    assert physical_locked, "Actual booking did not lock its physical master before reading availability"
                    availability_read.set()
                    await asyncio.wait_for(release_booking.wait(), timeout=10)
                return result

            monkeypatch.setattr(session, "execute", execute_with_gate)
            booking = await BookingServiceLayer().create_public_booking(session, PublicBookingCreate(
                master_id=booked_master, service_ids=[catalog["services"][booked_master]], start_at=start,
                customer_name="Actual synthetic booking", customer_phone="+380501234567"))
            return dict(status=201, booking_id=booking.id)

    async def delete():
        async with database() as session:
            pids["delete"] = await bounded_transaction(session)
            delete_ready.set()
            try:
                if allow_booked:
                    # The admin path intentionally allows deleting a window
                    # with a booking; keep that policy separate from the guard.
                    target = await session.get(MasterAvailabilityWindow, window_id)
                    await BookingServiceLayer().delete_availability_window(session, target, allow_booked=True)
                else:
                    await routes.delete_my_availability_window(window_id, current_master=await session.get(Master, delete_master),
                                                                session=session)
                return dict(status=204)
            except HTTPException as error:
                await session.rollback()
                return dict(status=error.status_code, detail=error.detail)

    booking_task = asyncio.create_task(book())
    delete_task = None
    try:
        await asyncio.wait_for(availability_read.wait(), timeout=5)
        delete_task = asyncio.create_task(delete())
        await asyncio.wait_for(delete_ready.wait(), timeout=5)
        assert len(set(pids.values())) == 2
        blocked = await wait_for_block_or_completion(database, pids["booking"], pids["delete"], delete_task)
        # Legacy deletion is already committed; fixed deletion is waiting on
        # the real booking's master lock. Both placements precede booking commit.
        release_booking.set()
        outcomes = await asyncio.wait_for(asyncio.gather(booking_task, delete_task), timeout=20)
        async with database() as verify:
            windows = list((await verify.scalars(select(MasterAvailabilityWindow))).all())
            bookings = list((await verify.scalars(select(Booking))).all())
            assert len(bookings) == 1 and bookings[0].id == outcomes[0]["booking_id"]
            assert bookings[0].status == BookingStatus.confirmed and bookings[0].master_id == catalog["physical"]
            assert bookings[0].redirected_from_master_id == (booked_master if redirected else None)
            assert bookings[0].service_id == catalog["services"][catalog["physical"]]
            assert bookings[0].start_at == start and bookings[0].end_at == start + timedelta(minutes=30)
        record_property("availability_delete_booking_race", json.dumps(dict(pids=pids, blocked=blocked,
                        allow_booked=allow_booked, outcomes=outcomes, windows=len(windows), bookings=len(bookings))))
        assert outcomes[0]["status"] == 201
        if allow_booked:
            assert outcomes[1] == dict(status=204) and windows == []
        else:
            assert outcomes[1] == dict(status=409, detail="Availability window has active bookings")
            assert len(windows) == 1 and windows[0].id == window_id
            assert blocked, "Guarded deletion must serialize behind the actual booking's physical-master lock"
    finally:
        release_booking.set()
        tasks = [task for task in (booking_task, delete_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_independent_master_availability_does_not_wait_for_another_calendar(database):
    catalog = await seed(database)
    ready = asyncio.Event()
    writer_pid = None

    async def create():
        nonlocal writer_pid
        async with database() as session:
            writer_pid = await bounded_transaction(session)
            ready.set()
            return await create_for_master(session, catalog["independent"], "window", catalog["start"],
                                           catalog["start"] + timedelta(hours=1))

    async with database() as holder:
        holder_pid = await bounded_transaction(holder)
        await holder.execute(select(Master.id).where(Master.id == catalog["physical"]).with_for_update())
        task = asyncio.create_task(create())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            blocked = await wait_for_block_or_completion(database, holder_pid, writer_pid, task)
            await holder.rollback()
            ids = await asyncio.wait_for(task, timeout=10)
            assert not blocked and len(ids) == 1
        finally:
            await holder.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with database() as verify:
        rows = list((await verify.scalars(select(MasterAvailabilityWindow))).all())
        assert len(rows) == 1 and rows[0].master_id == catalog["independent"]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["late-overlap", "duplicate-date"])
async def test_failed_days_batch_rolls_back_all_earlier_windows(database, failure):
    catalog = await seed(database)
    first = catalog["start"].date()
    second = first + timedelta(days=1)
    while second.weekday() == 0:
        second += timedelta(days=1)
    existing_id = None
    if failure == "late-overlap":
        async with database() as setup:
            start = datetime(second.year, second.month, second.day, 10, tzinfo=KYIV_TZ)
            existing = MasterAvailabilityWindow(master_id=catalog["physical"], start_at=start, end_at=start + timedelta(hours=1))
            setup.add(existing)
            await setup.commit()
            existing_id = existing.id
    async with database() as session:
        master = await session.get(Master, catalog["physical"])
        with pytest.raises(HTTPException) as rejected:
            await BookingServiceLayer().create_availability_days(session, master,
                [first, second] if failure == "late-overlap" else [first, first])
        assert rejected.value.status_code == 409
        assert rejected.value.detail == "Availability window overlaps an existing window"
        # Service rollback must leave the same session usable, not require the
        # caller to repair a partially written batch or failed transaction.
        rows = list((await session.scalars(select(MasterAvailabilityWindow))).all())
        assert [row.id for row in rows] == ([existing_id] if existing_id is not None else [])
        assert all(row.start_at.date() != first for row in rows)


@pytest.mark.anyio
async def test_inactive_redirect_source_still_writes_only_its_physical_calendar(database):
    catalog = await seed(database)
    async with database() as session:
        ids = await create_for_master(session, catalog["inactive"], "window", catalog["start"],
                                      catalog["start"] + timedelta(hours=1))
    async with database() as verify:
        rows = list((await verify.scalars(select(MasterAvailabilityWindow))).all())
        assert [row.id for row in rows] == ids
        assert len(rows) == 1 and rows[0].master_id == catalog["physical"]


@pytest.mark.anyio
async def test_rejected_busy_deletion_releases_master_lock_and_leaves_session_usable(database):
    catalog = await seed(database)
    start = catalog["start"]
    async with database() as setup:
        window = MasterAvailabilityWindow(master_id=catalog["physical"], start_at=start,
                                          end_at=start + timedelta(hours=1))
        setup.add(window)
        await setup.commit()
        window_id = window.id
        booking = await BookingServiceLayer().create_public_booking(setup, PublicBookingCreate(
            master_id=catalog["physical"], service_ids=[catalog["services"][catalog["physical"]]],
            start_at=start, customer_name="Synthetic cleanup booking", customer_phone="+380501234568"))
        booking_id = booking.id
    async with database() as rejected_session:
        target = await rejected_session.get(MasterAvailabilityWindow, window_id)
        with pytest.raises(HTTPException) as rejected:
            await BookingServiceLayer().delete_availability_window(rejected_session, target, allow_booked=False)
        assert rejected.value.status_code == 409
        assert rejected.value.detail == "Availability window has active bookings"
        # Keep this session open: connection closure must not be what releases
        # the newly introduced master lock after a business validation error.
        assert await rejected_session.scalar(text("SELECT 1")) == 1
        first_pid = await rejected_session.scalar(text("SELECT pg_backend_pid()"))
        async with database() as other:
            other_pid = await bounded_transaction(other)
            assert other_pid != first_pid
            locked = await other.scalar(select(Master.id).where(Master.id == catalog["physical"])
                                         .with_for_update(nowait=True))
            assert locked == catalog["physical"]
        assert await rejected_session.get(MasterAvailabilityWindow, window_id) is not None
        persisted = list((await rejected_session.scalars(select(Booking))).all())
        assert len(persisted) == 1 and persisted[0].id == booking_id
        assert persisted[0].status == BookingStatus.confirmed
    async with database() as verify:
        windows = list((await verify.scalars(select(MasterAvailabilityWindow))).all())
        bookings = list((await verify.scalars(select(Booking))).all())
        assert [row.id for row in windows] == [window_id]
        assert [row.id for row in bookings] == [booking_id]
