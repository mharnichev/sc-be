"""Real PostgreSQL expiry/cancel/claim lock races in a disposable local schema.

Only SEGMENTS_TEST_DATABASE_URL enables the shared guarded fixture. Providers
and application lifespan are never started. Gates wrap real SQL; the observer
proves PostgreSQL blocking, rather than substituting Python locks for row locks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models.booking import BarberService, Booking, BookingStatus, Master, MasterAvailabilityWindow
from app.models.booking_recovery import BookingRecoveryEvent
from app.models.customer import Customer
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.services.booking import KYIV_TZ
from app.services.waitlist import WaitlistService
from app.services.waitlist_offers import FreedBookingSlot, WaitlistOfferService
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


@pytest.fixture
def clock(monkeypatch):
    now = datetime.now(KYIV_TZ).replace(microsecond=0)
    monkeypatch.setattr(WaitlistOfferService, "_now", staticmethod(lambda: now))
    return now


async def seed(database, clock, *, count=1):
    day = clock.date() + timedelta(days=2)
    while day.weekday() == 0:
        day += timedelta(days=1)
    start = datetime(day.year, day.month, day.day, 10, tzinfo=KYIV_TZ)
    rows = []
    async with database() as session:
        master = Master(full_name="Expiry concurrency master", is_active=True)
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Expiry concurrency haircut", duration_minutes=30,
                                price=100, is_active=True)
        session.add(service)
        session.add(MasterAvailabilityWindow(master_id=master.id, start_at=start,
                                             end_at=start + timedelta(hours=4)))
        await session.flush()
        for index in range(count):
            customer = Customer(phone=f"+38050000{index:04d}", name=f"Synthetic waitlist {index}", is_active=True)
            session.add(customer)
            await session.flush()
            token, cancel_token = f"synthetic-offer-{index}", f"synthetic-cancel-{index}"
            request = WaitlistRequest(customer_id=customer.id, preferred_master_id=master.id,
                desired_date=start.date(), duration_minutes=30, notification_consent=True,
                expires_at=start, cancel_token_hash=WaitlistService._hash_token(cancel_token),
                dedup_key_hash=WaitlistService._hash_token(f"synthetic-dedup-{index}"),
                status=WaitlistStatus.offered, offered_at=clock, services=[service])
            session.add(request)
            await session.flush()
            offer = WaitlistOffer(request_id=request.id, master_id=master.id, source_master_id=master.id,
                start_at=start + timedelta(minutes=30 * index),
                end_at=start + timedelta(minutes=30 * (index + 1)), token_hash=WaitlistOfferService._hash(token),
                status=WaitlistOfferStatus.delivered, scheduled_at=clock, delivered_at=clock,
                expires_at=clock + timedelta(minutes=5))
            session.add(offer)
            await session.flush()
            rows.append(dict(request_id=request.id, offer_id=offer.id, master_id=master.id,
                             start_at=offer.start_at, end_at=offer.end_at, token=token, cancel_token=cancel_token))
        await session.commit()
    return rows


async def bounded_transaction(session):
    # Bounds are local to these disposable transactions; the observer releases
    # intentional waits well before lock_timeout. No privileged server settings.
    await session.execute(text("SET LOCAL lock_timeout = '10s'"))
    await session.execute(text("SET LOCAL statement_timeout = '15s'"))
    return await session.scalar(text("SELECT pg_backend_pid()"))


def locks_entity(statement, entity):
    lock = getattr(statement, "_for_update_arg", None)
    if lock is None:
        return False
    if lock.of is not None:
        return any((getattr(item, "name", None) or getattr(item, "__tablename__", None)) == entity.__tablename__
                   for item in lock.of)
    return any(item.get("entity") is entity for item in getattr(statement, "column_descriptions", []))


class ExpiryWriterRace:
    def __init__(self, database):
        self.database = database
        self.pids = {}
        self.writer_holds_request = asyncio.Event()
        self.expiry_holds_offer = asyncio.Event()
        self.release_writer = asyncio.Event()
        self.release_expiry = asyncio.Event()
        self.edges = []

    async def prepare(self, session, label, monkeypatch):
        self.pids[label] = await bounded_transaction(session)
        assert len(set(self.pids.values())) == len(self.pids)
        execute = session.execute

        async def execute_with_gate(statement, *args, **kwargs):
            result = await execute(statement, *args, **kwargs)
            if label == "writer" and locks_entity(statement, WaitlistRequest) and not self.writer_holds_request.is_set():
                self.writer_holds_request.set()
                await asyncio.wait_for(self.release_writer.wait(), timeout=10)
            elif label == "expiry" and locks_entity(statement, WaitlistOffer) and not self.expiry_holds_offer.is_set():
                # Frozen Result preserves real ORM rows while allowing the
                # gate to distinguish an empty SKIP LOCKED result from a lock.
                frozen = result.freeze()
                if frozen().scalars().all():
                    self.expiry_holds_offer.set()
                    await asyncio.wait_for(self.release_expiry.wait(), timeout=10)
                result = frozen()
            return result

        monkeypatch.setattr(session, "execute", execute_with_gate)

    async def observe(self, *, require_cycle, tasks):
        async with self.database() as observer:
            observer_pid = await observer.scalar(text("SELECT pg_backend_pid()"))
            assert observer_pid not in self.pids.values()
            async with asyncio.timeout(5):
                while True:
                    rows = (await observer.execute(text(
                        "SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                        "WHERE pid IN (:writer, :expiry)"
                    ), self.pids)).all()
                    blockers = {row.pid: row.blockers for row in rows}
                    writer, expiry = self.pids["writer"], self.pids["expiry"]
                    writer_waits = expiry in blockers.get(writer, [])
                    expiry_waits = writer in blockers.get(expiry, [])
                    if writer_waits:
                        edge = [writer, expiry]
                        if edge not in self.edges:
                            self.edges.append(edge)
                    if expiry_waits:
                        edge = [expiry, writer]
                        if edge not in self.edges:
                            self.edges.append(edge)
                    if writer_waits and (expiry_waits or not require_cycle):
                        return
                    # A server with a short deadlock_timeout may detect the
                    # cycle before this observer's next sample. Its 40P01 is
                    # still required below, never replaced with a timeout.
                    if require_cycle and any(task.done() for task in tasks):
                        return
                    await asyncio.sleep(0.01)


async def state(database, row):
    async with database() as session:
        request = await session.get(WaitlistRequest, row["request_id"])
        offer = await session.get(WaitlistOffer, row["offer_id"])
        bookings = list((await session.scalars(select(Booking))).all())
        return dict(request=request.status.value, offer=offer.status.value, bookings=len(bookings),
                    booking_ids=[item.id for item in bookings], close_reason=offer.close_reason)


@pytest.mark.anyio
@pytest.mark.parametrize("writer_kind", ["cancel", "claim"])
async def test_expiry_serializes_with_request_first_customer_writer_without_deadlock(
    database, clock, monkeypatch, record_property, writer_kind,
):
    row, = await seed(database, clock)
    race = ExpiryWriterRace(database)

    async def operation(label):
        async with database() as session:
            await race.prepare(session, label, monkeypatch)
            try:
                if label == "expiry":
                    expired = await WaitlistOfferService().expire_holds(session, now=clock + timedelta(minutes=10))
                    ids = [item.id for item in expired]
                    await session.commit()
                    return dict(operation=label, expired_ids=ids)
                if writer_kind == "cancel":
                    request, slots = await WaitlistService().cancel_with_slots(session, row["cancel_token"])
                    return dict(operation=label, request=request.status.value, slots=len(slots))
                booking = await WaitlistOfferService().claim(session, row["token"])
                return dict(operation=label, booking_id=booking.id)
            except DBAPIError as error:
                await session.rollback()
                return dict(operation=label, sqlstate=getattr(error.orig, "sqlstate", None)
                            or getattr(error.orig, "pgcode", None), database_error=str(error))

    writer = asyncio.create_task(operation("writer"))
    expiry = None
    offer_gate = None
    try:
        await asyncio.wait_for(race.writer_holds_request.wait(), timeout=5)
        expiry = asyncio.create_task(operation("expiry"))
        offer_gate = asyncio.create_task(race.expiry_holds_offer.wait())
        done, _ = await asyncio.wait({expiry, offer_gate}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert done, "Expiry neither skipped the locked request nor reached its offer lock"
        if offer_gate in done and race.expiry_holds_offer.is_set():
            # Legacy order: expiry owns offer; writer owns request. Confirm
            # writer is actually blocked on the offer before closing the cycle.
            race.release_writer.set()
            await race.observe(require_cycle=False, tasks=[writer, expiry])
            assert [race.pids["writer"], race.pids["expiry"]] in race.edges
            race.release_expiry.set()
            await race.observe(require_cycle=True, tasks=[writer, expiry])
        else:
            # Request-first SKIP LOCKED expiry must finish without acquiring
            # this offer, so the customer transaction can complete normally.
            race.release_writer.set()
            race.release_expiry.set()
        outcomes = await asyncio.wait_for(asyncio.gather(writer, expiry), timeout=20)
        final = await state(database, row)
        diagnostics = dict(pids=race.pids, blocking_edges=race.edges, outcomes=outcomes, final=final)
        record_property(f"expiry_{writer_kind}_race", json.dumps(diagnostics, sort_keys=True))
        errors = [item for item in outcomes if "database_error" in item]
        assert all(item["sqlstate"] == "40P01" for item in errors), diagnostics
        assert not errors, f"Real PostgreSQL expiry/customer deadlock: {diagnostics}"
        assert outcomes[1]["expired_ids"] == []
        if writer_kind == "cancel":
            assert final == dict(request="cancelled", offer="cancelled", bookings=0, booking_ids=[],
                                 close_reason="request_cancelled_by_customer")
            assert outcomes[0]["slots"] == 1
        else:
            assert final["request"] == "booked" and final["offer"] == "claimed" and final["bookings"] == 1
            assert final["booking_ids"] == [outcomes[0]["booking_id"]]
    finally:
        race.release_writer.set()
        race.release_expiry.set()
        tasks = [task for task in (writer, expiry, offer_gate) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def blocked_or_finished(database, holder_pid, waiter_pid, task):
    async with database() as observer:
        async with asyncio.timeout(5):
            while not task.done():
                blocked = await observer.scalar(text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"),
                                                {"holder": holder_pid, "waiter": waiter_pid})
                if blocked:
                    return True
                await asyncio.sleep(0.01)
    return False


@pytest.mark.anyio
@pytest.mark.parametrize("locked_entity", [WaitlistRequest, WaitlistOffer], ids=["locked-request", "locked-offer"])
async def test_expiry_batch_skips_locked_pair_and_processes_independent_due_hold(
    database, clock, locked_entity, record_property,
):
    first, second = await seed(database, clock, count=2)
    pid_ready = asyncio.Event()
    expiry_pid = None

    async def expire():
        nonlocal expiry_pid
        async with database() as session:
            expiry_pid = await bounded_transaction(session)
            pid_ready.set()
            offers = await WaitlistOfferService().expire_holds(session, now=clock + timedelta(minutes=10))
            ids = [item.id for item in offers]
            await session.commit()
            return ids

    async with database() as holder:
        holder_pid = await bounded_transaction(holder)
        key = first["request_id"] if locked_entity is WaitlistRequest else first["offer_id"]
        await holder.execute(select(locked_entity).where(locked_entity.id == key).with_for_update())
        task = asyncio.create_task(expire())
        try:
            await asyncio.wait_for(pid_ready.wait(), timeout=5)
            assert holder_pid != expiry_pid
            blocked = await blocked_or_finished(database, holder_pid, expiry_pid, task)
            await holder.rollback()
            expired_ids = await asyncio.wait_for(task, timeout=10)
            record_property(f"expiry_batch_{locked_entity.__tablename__}", json.dumps(
                dict(holder_pid=holder_pid, expiry_pid=expiry_pid, blocked=blocked, expired_ids=expired_ids)))
            assert not blocked, "Batch expiry waited on a locked request/offer instead of skipping it"
            assert expired_ids == [second["offer_id"]]
        finally:
            await holder.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert (await state(database, first))["offer"] == "delivered"
    assert (await state(database, first))["request"] == "offered"
    assert (await state(database, second))["offer"] == "expired"
    assert (await state(database, second))["request"] == "active"
    async with database() as session:
        remaining = await WaitlistOfferService().expire_holds(session, now=clock + timedelta(minutes=10))
        assert [item.id for item in remaining] == [first["offer_id"]]
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(BookingRecoveryEvent)) == 2


@pytest.mark.anyio
async def test_expiry_respects_current_terminal_request_after_lock_not_stale_identity_map(database, clock):
    row, = await seed(database, clock)
    async with database() as scheduler:
        stale = await scheduler.get(WaitlistRequest, row["request_id"])
        assert stale.status == WaitlistStatus.offered
        async with database() as writer:
            request = await writer.get(WaitlistRequest, row["request_id"], with_for_update=True)
            request.status = WaitlistStatus.cancelled
            request.closed_at = clock
            request.close_reason = "synthetic-terminal-state"
            await writer.commit()
        # A historical terminal request can retain an active offer. Expire the
        # hold, but never resurrect its currently cancelled parent request.
        expired = await WaitlistOfferService().expire_holds(scheduler, now=clock + timedelta(minutes=10))
        assert [item.id for item in expired] == [row["offer_id"]]
        await scheduler.commit()
    async with database() as verify:
        request = await verify.get(WaitlistRequest, row["request_id"])
        assert request.status == WaitlistStatus.cancelled
        assert request.closed_at == clock and request.close_reason == "synthetic-terminal-state"


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["extended", "claimed"])
async def test_expiry_rechecks_current_offer_eligibility_despite_preloaded_old_row(database, clock, change):
    row, = await seed(database, clock)
    deadline = clock + timedelta(minutes=10)
    async with database() as scheduler:
        stale = await scheduler.get(WaitlistOffer, row["offer_id"])
        assert stale.status == WaitlistOfferStatus.delivered and stale.expires_at < deadline
        async with database() as writer:
            offer = await writer.get(WaitlistOffer, row["offer_id"], with_for_update=True)
            if change == "extended":
                offer.expires_at = deadline + timedelta(microseconds=1)
            else:
                offer.status = WaitlistOfferStatus.claimed
                request = await writer.get(WaitlistRequest, row["request_id"])
                request.status = WaitlistStatus.booked
            await writer.commit()
        assert await WaitlistOfferService().expire_holds(scheduler, now=deadline) == []
        await scheduler.commit()
    async with database() as verify:
        offer = await verify.get(WaitlistOffer, row["offer_id"])
        assert offer.status == (WaitlistOfferStatus.delivered if change == "extended" else WaitlistOfferStatus.claimed)
        assert offer.expires_at == (deadline + timedelta(microseconds=1) if change == "extended" else clock + timedelta(minutes=5))
        assert await verify.scalar(select(func.count()).select_from(BookingRecoveryEvent)) == 0


@pytest.mark.anyio
async def test_expiry_status_deadline_boundaries_and_repeat_are_atomic(database, clock):
    rows = await seed(database, clock, count=8)
    deadline = clock + timedelta(minutes=10)
    statuses = list(WaitlistOfferStatus)
    async with database() as setup:
        for row, status in zip(rows, statuses):
            offer = await setup.get(WaitlistOffer, row["offer_id"])
            offer.status = status
        future = await setup.get(WaitlistOffer, rows[6]["offer_id"])
        future.expires_at = deadline + timedelta(microseconds=1)
        exact = await setup.get(WaitlistOffer, rows[7]["offer_id"])
        exact.expires_at = deadline
        await setup.commit()
    eligible = [rows[1]["offer_id"], rows[2]["offer_id"], rows[7]["offer_id"]]
    async with database() as session:
        offers = await WaitlistOfferService().expire_holds(session, now=deadline)
        assert sorted(item.id for item in offers) == sorted(eligible)
        await session.commit()
    async with database() as verify:
        for row in rows:
            offer = await verify.get(WaitlistOffer, row["offer_id"])
            request = await verify.get(WaitlistRequest, row["request_id"])
            if row["offer_id"] in eligible:
                assert offer.status == WaitlistOfferStatus.expired
                assert offer.closed_at == deadline and offer.close_reason == "hold_expired"
                assert request.status == WaitlistStatus.active and request.offered_at is None
            else:
                assert request.status == WaitlistStatus.offered and request.offered_at == clock
        assert await WaitlistOfferService().expire_holds(verify, now=deadline) == []
        assert await verify.scalar(select(func.count()).select_from(BookingRecoveryEvent)) == len(eligible)


@pytest.mark.anyio
async def test_expiry_commits_released_hold_before_reoffer_boundary(database, clock, monkeypatch):
    row, = await seed(database, clock)
    service = WaitlistOfferService()
    monkeypatch.setattr(service, "_now", lambda: clock + timedelta(minutes=10))
    calls = []

    async def offer_slot(session, **kwargs):
        assert not session.in_transaction(), "Reoffer started before the expiry transaction committed"
        async with database() as observer:
            offer = (await observer.execute(select(WaitlistOffer).where(WaitlistOffer.id == row["offer_id"])
                                            .with_for_update(nowait=True))).scalar_one()
            assert offer.status == WaitlistOfferStatus.expired and offer.close_reason == "hold_expired"
            request = await observer.get(WaitlistRequest, row["request_id"], with_for_update={"nowait": True})
            assert request.status == WaitlistStatus.active and request.offered_at is None
        calls.append(FreedBookingSlot(**kwargs))
        return None  # Record only; matching/sending providers are outside this test.

    monkeypatch.setattr(service, "offer_slot", offer_slot)
    async with database() as session:
        assert await service.expire_and_offer_next(session) == 0
    assert calls == [FreedBookingSlot(master_id=row["master_id"], start_at=row["start_at"], end_at=row["end_at"],
                                     source_master_id=row["master_id"], source_booking_id=None)]


@pytest.mark.anyio
async def test_claim_is_one_time_and_later_expiry_cannot_reopen_booked_request(database, clock):
    row, = await seed(database, clock)
    async with database() as session:
        booking = await WaitlistOfferService().claim(session, row["token"])
        booking_id = booking.id
    async with database() as again:
        with pytest.raises(HTTPException) as rejected:
            await WaitlistOfferService().claim(again, row["token"])
        assert rejected.value.status_code == 410
        await again.rollback()
    async with database() as scheduler:
        assert await WaitlistOfferService().expire_holds(scheduler, now=clock + timedelta(minutes=10)) == []
        booking = await scheduler.get(Booking, booking_id)
        assert booking.status == BookingStatus.confirmed
    final = await state(database, row)
    assert final["request"] == "booked" and final["offer"] == "claimed" and final["bookings"] == 1
    assert final["booking_ids"] == [booking_id]
