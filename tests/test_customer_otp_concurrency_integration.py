"""OTP races against the real service in opt-in, isolated PostgreSQL schemas."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.security import hash_otp_code
from app.models.customer import Customer
from app.models.customer_otp_code import CustomerOtpCode
from app.services.customer_auth import CustomerAuthService, OtpVerifyResult
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


PHONE = "+380500001234"
FORMATTED_PHONE = "+38 (050) 000-12-34"
CODE = "123456"
WRONG_CODE = "654321"
MAX_ATTEMPTS = 3
MISSING = "OTP code is missing or expired"
INVALID = "Invalid OTP code"
LIMIT = "Daily OTP verification attempt limit reached for this phone number"


@pytest.fixture(autouse=True)
def forbid_notifications_and_fix_limit(monkeypatch):
    monkeypatch.setattr(settings, "otp_max_verify_attempts_per_day", MAX_ATTEMPTS)

    async def forbidden_sms(*args, **kwargs):
        raise AssertionError("OTP verification must not send notifications")

    monkeypatch.setattr("app.services.sms.SmsService.send_otp_code", forbidden_sms)
    monkeypatch.setattr("app.services.sms.SmsService.send_message", forbidden_sms)


async def seed_otp(database, *, account="exact", latest_attempts=0, prior_attempts=0, latest_state="valid"):
    # Derive times from now so TTL and UTC daily accounting retain production semantics.
    now = datetime.now(UTC)
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    async with database() as session:
        customer = None
        if account != "new":
            customer = Customer(phone=PHONE if account == "exact" else "+0500001234", is_active=True)
            session.add(customer)
            await session.flush()
        prior = None
        if prior_attempts or latest_state in {"expired", "used"}:
            prior = CustomerOtpCode(
                phone=PHONE, code_hash=hash_otp_code(PHONE, CODE), attempts_count=prior_attempts,
                sent_at=day_start, expires_at=now + timedelta(minutes=10),
            )
            session.add(prior)
        latest = None
        if latest_state != "missing":
            latest = CustomerOtpCode(
                phone=PHONE, code_hash=hash_otp_code(PHONE, CODE), attempts_count=latest_attempts,
                sent_at=now, expires_at=now + timedelta(minutes=10),
                verified_at=now if latest_state == "used" else None,
            )
            if latest_state == "expired":
                latest.expires_at = now - timedelta(seconds=1)
            session.add(latest)
        await session.commit()
        return SimpleNamespace(
            customer_id=customer.id if customer is not None else None,
            latest_id=latest.id if latest is not None else None,
            prior_id=prior.id if prior is not None else None,
        )


class LatestReadRace:
    """Force the unlocked race; for locking SQL prove an actual blocked backend.

    A barrier after two unlocked SELECT results gives both verifiers the same
    pre-update row. With FOR UPDATE that placement would deadlock, so the first
    locked reader is held until an observer sees the other PostgreSQL backend
    waiting on its row or earlier phone lock. Outcomes remain identical.
    """

    def __init__(self, database, *, wrong_before_correct=False):
        self.database = database
        self.wrong_before_correct = wrong_before_correct
        self.ready = asyncio.Barrier(2)
        self.read_barrier = asyncio.Barrier(2)
        self.mode_known = asyncio.Event()
        self.first_locked_read = asyncio.Event()
        self.release_locked_reader = asyncio.Event()
        self.wrong_finished = asyncio.Event()
        self.pids = {}
        self.started_phone_lock = []
        self.started_latest = []
        self.completed_reads = []
        self.locked = None
        self.first_holder = None
        self.blocked_pair = None

    async def prepare(self, session, label, monkeypatch):
        execute = session.execute
        self.pids[label] = (await execute(select(func.pg_backend_pid()))).scalar_one()
        assert len(set(self.pids.values())) == len(self.pids), "Race requires separate PostgreSQL connections"

        async def intercept(statement, *args, **kwargs):
            if "pg_advisory_xact_lock" in str(statement).lower():
                self.started_phone_lock.append(label)
                return await execute(statement, *args, **kwargs)
            descriptions = getattr(statement, "column_descriptions", [])
            is_latest = (
                any(item.get("entity") is CustomerOtpCode for item in descriptions)
                and getattr(statement, "_limit_clause", None) is not None
                and bool(getattr(statement, "_order_by_clauses", ()))
            )
            if not is_latest:
                return await execute(statement, *args, **kwargs)
            locked = getattr(statement, "_for_update_arg", None) is not None
            if self.locked is None:
                self.locked = locked
                self.mode_known.set()
            assert self.locked == locked
            self.started_latest.append(label)
            result = await execute(statement, *args, **kwargs)
            self.completed_reads.append(label)
            if locked:
                if self.first_holder is None:
                    self.first_holder = label
                    self.first_locked_read.set()
                    await asyncio.wait_for(self.release_locked_reader.wait(), timeout=10)
            else:
                await asyncio.wait_for(self.read_barrier.wait(), timeout=10)
                if self.wrong_before_correct and label == "correct":
                    await asyncio.wait_for(self.wrong_finished.wait(), timeout=10)
            return result

        monkeypatch.setattr(session, "execute", intercept)
        await asyncio.wait_for(self.ready.wait(), timeout=10)

    async def observe_lock(self):
        await asyncio.wait_for(self.mode_known.wait(), timeout=10)
        if not self.locked:
            return
        await asyncio.wait_for(self.first_locked_read.wait(), timeout=10)
        holder_pid = self.pids[self.first_holder]
        waiter_pid = next(pid for label, pid in self.pids.items() if label != self.first_holder)
        async with self.database() as observer:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                wait_event = await observer.scalar(text(
                    "SELECT wait_event FROM pg_stat_activity "
                    "WHERE pid = :waiter AND wait_event_type = 'Lock' "
                    "AND :holder = ANY(pg_blocking_pids(pid))"
                ), {"waiter": waiter_pid, "holder": holder_pid})
                if wait_event is not None:
                    assert len(self.pids) == 2
                    if self.started_phone_lock:
                        assert len(self.started_phone_lock) == 2
                        assert wait_event == "advisory"
                        assert len(self.started_latest) == 1
                    else:
                        assert len(self.started_latest) == 2
                    self.blocked_pair = (holder_pid, waiter_pid)
                    self.release_locked_reader.set()
                    return
                await asyncio.sleep(0.01)
        raise AssertionError("Second verifier never blocked on the first PostgreSQL transaction")

    def assert_interleaving(self):
        assert len(self.pids) == 2
        assert len(set(self.pids.values())) == 2
        assert len(self.started_latest) == 2
        assert len(self.completed_reads) == 2
        if self.locked:
            assert self.blocked_pair is not None
            assert self.blocked_pair[0] != self.blocked_pair[1]
        else:
            assert self.read_barrier.n_waiting == 0


async def race_verifications(database, monkeypatch, requests, *, wrong_before_correct=False):
    race = LatestReadRace(database, wrong_before_correct=wrong_before_correct)

    async def attempt(label, phone, code):
        try:
            async with database() as session:
                await race.prepare(session, label, monkeypatch)
                try:
                    return await CustomerAuthService().verify_otp(session, phone, code)
                except HTTPException as error:
                    return error
        finally:
            if label == "wrong":
                race.wrong_finished.set()

    tasks = [asyncio.create_task(attempt(*request)) for request in requests]
    observer = asyncio.create_task(race.observe_lock())
    all_tasks = [*tasks, observer]
    try:
        values = await asyncio.wait_for(asyncio.gather(*all_tasks), timeout=20)
        race.assert_interleaving()
        return dict(zip((request[0] for request in requests), values[:2])), race
    finally:
        race.release_locked_reader.set()
        for task in all_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


def assert_error(value, code, detail):
    assert isinstance(value, HTTPException), value
    assert (value.status_code, value.detail) == (code, detail)


@pytest.mark.anyio
@pytest.mark.parametrize("account", ["exact", "legacy-alias", "new"], ids=["login", "legacy-login", "signup"])
async def test_concurrent_correct_code_is_consumed_once(database, monkeypatch, account):
    seed = await seed_otp(database, account=account)
    outcomes, race = await race_verifications(database, monkeypatch, [("first", PHONE, CODE), ("second", FORMATTED_PHONE, CODE)])
    successes = [value for value in outcomes.values() if isinstance(value, OtpVerifyResult)]
    assert len(successes) == 1, f"Expected one consume, got {len(successes)}; completed latest reads={race.completed_reads}"
    failures = [value for value in outcomes.values() if isinstance(value, HTTPException)]
    assert len(failures) == 1
    assert_error(failures[0], 400, MISSING)
    result = successes[0]
    assert result.access_token
    assert result.is_new_customer is (account == "new")
    assert result.attempts_left_today == MAX_ATTEMPTS - 1
    async with database() as session:
        otp = await session.get(CustomerOtpCode, seed.latest_id)
        assert otp.verified_at is not None
        assert otp.last_attempt_at is not None
        assert otp.attempts_count == 1
        customers = list(await session.scalars(select(Customer)))
        assert len(customers) == 1
        customer = customers[0]
        assert customer.phone == ("+0500001234" if account == "legacy-alias" else PHONE)
        assert customer.id == (seed.customer_id or result.customer.id)
        assert customer.phone_verified_at is not None
        assert customer.last_login_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize("second_phone", [PHONE, FORMATTED_PHONE], ids=["same-phone", "normalized-phone"])
async def test_two_concurrent_wrong_attempts_both_increment(database, monkeypatch, second_phone):
    seed = await seed_otp(database)
    outcomes, _ = await race_verifications(database, monkeypatch, [("first", PHONE, WRONG_CODE), ("second", second_phone, WRONG_CODE)])
    for outcome in outcomes.values():
        assert_error(outcome, 400, INVALID)
    async with database() as session:
        otp = await session.get(CustomerOtpCode, seed.latest_id)
        assert otp.attempts_count == 2
        assert otp.verified_at is None
        assert otp.last_attempt_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize("prior_record", [False, True], ids=["latest-counter", "daily-sum"])
async def test_concurrent_wrong_attempts_cannot_both_cross_daily_threshold(database, monkeypatch, prior_record):
    seed = await seed_otp(
        database, latest_attempts=0 if prior_record else MAX_ATTEMPTS - 1,
        prior_attempts=MAX_ATTEMPTS - 1 if prior_record else 0,
    )
    outcomes, _ = await race_verifications(database, monkeypatch, [("first", PHONE, WRONG_CODE), ("second", PHONE, WRONG_CODE)])
    assert sorted((value.status_code, value.detail) for value in outcomes.values()) == [(400, INVALID), (429, LIMIT)]
    async with database() as session:
        otp = await session.get(CustomerOtpCode, seed.latest_id)
        assert otp.verified_at is None
        assert otp.attempts_count == (1 if prior_record else MAX_ATTEMPTS)
        assert await session.scalar(select(func.sum(CustomerOtpCode.attempts_count))) == MAX_ATTEMPTS
        if prior_record:
            assert (await session.get(CustomerOtpCode, seed.prior_id)).attempts_count == MAX_ATTEMPTS - 1


@pytest.mark.anyio
@pytest.mark.parametrize("prior_record", [False, True], ids=["latest-counter", "daily-sum"])
async def test_correct_code_cannot_use_a_slot_consumed_by_concurrent_wrong_attempt(database, monkeypatch, prior_record):
    seed = await seed_otp(
        database, latest_attempts=0 if prior_record else MAX_ATTEMPTS - 1,
        prior_attempts=MAX_ATTEMPTS - 1 if prior_record else 0,
    )
    outcomes, race = await race_verifications(
        database, monkeypatch, [("wrong", PHONE, WRONG_CODE), ("correct", PHONE, CODE)], wrong_before_correct=True,
    )
    correct_won_lock = race.locked and race.first_holder == "correct"
    if correct_won_lock:
        assert isinstance(outcomes["correct"], OtpVerifyResult)
        assert outcomes["correct"].attempts_left_today == 0
        # The daily gate precedes checking whether the latest OTP was consumed.
        assert_error(outcomes["wrong"], 429, LIMIT)
    else:
        assert_error(outcomes["wrong"], 400, INVALID)
        assert_error(outcomes["correct"], 429, LIMIT)
    async with database() as session:
        otp = await session.get(CustomerOtpCode, seed.latest_id)
        assert (otp.verified_at is not None) == bool(correct_won_lock)
        assert otp.attempts_count == (1 if prior_record else MAX_ATTEMPTS)
        assert await session.scalar(select(func.sum(CustomerOtpCode.attempts_count))) == MAX_ATTEMPTS


@pytest.mark.anyio
async def test_already_exhausted_daily_counter_rejects_correct_and_wrong_without_mutation(database):
    seed = await seed_otp(database, prior_attempts=MAX_ATTEMPTS)

    async def attempt(code):
        async with database() as session:
            with pytest.raises(HTTPException) as error:
                await CustomerAuthService().verify_otp(session, PHONE, code)
            assert_error(error.value, 429, LIMIT)

    tasks = [asyncio.create_task(attempt(code)) for code in [CODE, WRONG_CODE]]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    async with database() as session:
        latest = await session.get(CustomerOtpCode, seed.latest_id)
        assert latest.attempts_count == 0
        assert latest.verified_at is None
        assert latest.last_attempt_at is None
        assert (await session.get(CustomerOtpCode, seed.prior_id)).attempts_count == MAX_ATTEMPTS


@pytest.mark.anyio
@pytest.mark.parametrize("latest_state", ["expired", "used", "missing"])
async def test_latest_used_or_expired_code_never_revives_an_older_valid_code(database, latest_state):
    seed = await seed_otp(database, latest_state=latest_state)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await CustomerAuthService().verify_otp(session, PHONE, CODE)
        assert_error(error.value, 400, MISSING)
    async with database() as session:
        if seed.prior_id is not None:
            prior = await session.get(CustomerOtpCode, seed.prior_id)
            assert prior.verified_at is None
            assert prior.attempts_count == 0
            assert prior.last_attempt_at is None
        if seed.latest_id is not None:
            latest = await session.get(CustomerOtpCode, seed.latest_id)
            assert latest.attempts_count == 0
            assert latest.last_attempt_at is None
            assert (latest.verified_at is not None) == (latest_state == "used")


@pytest.mark.anyio
async def test_sequential_replay_preserves_missing_error_and_one_attempt(database):
    seed = await seed_otp(database)
    async with database() as session:
        result = await CustomerAuthService().verify_otp(session, PHONE, CODE)
        assert result.access_token
        assert result.is_new_customer is False
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await CustomerAuthService().verify_otp(session, FORMATTED_PHONE, CODE)
        assert_error(error.value, 400, MISSING)
    async with database() as session:
        otp = await session.get(CustomerOtpCode, seed.latest_id)
        assert otp.attempts_count == 1
        assert otp.verified_at is not None


@pytest.mark.anyio
async def test_preloaded_otp_is_refreshed_after_another_session_consumes_it(database):
    seed = await seed_otp(database)
    async with database() as stale_session:
        cached = await stale_session.get(CustomerOtpCode, seed.latest_id)
        assert cached.verified_at is None
        async with database() as consuming_session:
            result = await CustomerAuthService().verify_otp(consuming_session, PHONE, CODE)
            assert result.access_token
        # Keep the original ORM instance alive to exercise identity-map refresh.
        assert cached.verified_at is None
        with pytest.raises(HTTPException) as error:
            await CustomerAuthService().verify_otp(stale_session, PHONE, CODE)
        assert_error(error.value, 400, MISSING)
    async with database() as session:
        otp = await session.get(CustomerOtpCode, seed.latest_id)
        assert otp.attempts_count == 1
        assert otp.verified_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize("code", [WRONG_CODE, CODE], ids=["wrong-attempts", "correct-consumes"])
async def test_new_generation_cannot_bypass_phone_daily_attempt_cap(database, monkeypatch, code):
    seed = await seed_otp(database, prior_attempts=MAX_ATTEMPTS - 1)
    first_selected = asyncio.Event()
    generation_inserted = asyncio.Event()
    second_connected = asyncio.Event()
    first_budget_read = asyncio.Event()
    release_phone_lock_holder = asyncio.Event()
    budget_barrier = asyncio.Barrier(2)
    backend_pids = {}
    selected_ids = {}
    budget_reads = {}
    advisory_started = set()
    blocked_pair = None

    async def attempt(label):
        async with database() as session:
            execute = session.execute
            scalar = session.scalar
            backend_pids[label] = (await execute(select(func.pg_backend_pid()))).scalar_one()
            if label == "second":
                second_connected.set()

            async def intercept_sql(statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement).lower():
                    advisory_started.add(label)
                return await execute(statement, *args, **kwargs)

            async def intercept_scalar(statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement).lower():
                    advisory_started.add(label)
                return await scalar(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", intercept_sql)
            monkeypatch.setattr(session, "scalar", intercept_scalar)
            service = CustomerAuthService()
            latest = service._latest_otp
            attempts_count = service._attempts_count_today

            async def observe_latest(*args, **kwargs):
                row = await latest(*args, **kwargs)
                selected_ids[label] = row.id if row is not None else None
                if label == "first":
                    assert selected_ids[label] == seed.latest_id
                    first_selected.set()
                    await asyncio.wait_for(generation_inserted.wait(), timeout=10)
                return row

            async def hold_budget(*args, **kwargs):
                count = await attempts_count(*args, **kwargs)
                budget_reads[label] = count
                if "first" in advisory_started:
                    # A future phone lock must prove that another backend is
                    # blocked before the first verifier can update or commit.
                    if label == "first":
                        first_budget_read.set()
                        await asyncio.wait_for(release_phone_lock_holder.wait(), timeout=10)
                else:
                    # Current row locks protect different generations, so both
                    # real SUM results must be captured before either update.
                    await asyncio.wait_for(budget_barrier.wait(), timeout=10)
                return count

            monkeypatch.setattr(service, "_latest_otp", observe_latest)
            monkeypatch.setattr(service, "_attempts_count_today", hold_budget)
            try:
                outcome = await service.verify_otp(session, PHONE, code)
            except HTTPException as error:
                outcome = error
            assert await session.scalar(select(1)) == 1, "Verifier must leave its session usable"
            return outcome

    async def observe_phone_lock():
        nonlocal blocked_pair
        await asyncio.wait_for(second_connected.wait(), timeout=10)
        await asyncio.wait_for(first_budget_read.wait(), timeout=10)
        holder_pid, waiter_pid = backend_pids["first"], backend_pids["second"]
        assert holder_pid != waiter_pid
        async with database() as observer:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                blocked = await observer.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE pid = :waiter AND wait_event_type = 'Lock' "
                    "AND wait_event = 'advisory' "
                    "AND :holder = ANY(pg_blocking_pids(pid)))"
                ), {"waiter": waiter_pid, "holder": holder_pid})
                if blocked:
                    assert advisory_started == {"first", "second"}
                    blocked_pair = (holder_pid, waiter_pid)
                    release_phone_lock_holder.set()
                    return
                await asyncio.sleep(0.01)
        raise AssertionError("New-generation verifier did not block on the first phone transaction lock")

    tasks = [asyncio.create_task(attempt("first"))]
    try:
        await asyncio.wait_for(first_selected.wait(), timeout=10)
        # Insert through a separate real transaction while the first verifier
        # still owns the previous generation's row lock. No request/send runs.
        async with database() as supervisor:
            now = datetime.now(UTC)
            new_generation = CustomerOtpCode(
                phone=PHONE, code_hash=hash_otp_code(PHONE, CODE), attempts_count=0,
                sent_at=now, expires_at=now + timedelta(minutes=10),
            )
            supervisor.add(new_generation)
            await supervisor.commit()
            new_id = new_generation.id
            assert new_id != seed.latest_id
        generation_inserted.set()
        tasks.append(asyncio.create_task(attempt("second")))
        if "first" in advisory_started:
            tasks.append(asyncio.create_task(observe_phone_lock()))
        values = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
        first_outcome, second_outcome = values[:2]
        assert len(set(backend_pids.values())) == 2
        assert selected_ids == {"first": seed.latest_id, "second": new_id}
        if advisory_started:
            assert blocked_pair == (backend_pids["first"], backend_pids["second"])
            assert budget_reads == {"first": MAX_ATTEMPTS - 1, "second": MAX_ATTEMPTS}
        else:
            assert budget_reads == {"first": MAX_ATTEMPTS - 1, "second": MAX_ATTEMPTS - 1}
            assert budget_barrier.n_waiting == 0
        async with database() as session:
            prior = await session.get(CustomerOtpCode, seed.prior_id)
            old = await session.get(CustomerOtpCode, seed.latest_id)
            new = await session.get(CustomerOtpCode, new_id)
            assert prior.attempts_count == MAX_ATTEMPTS - 1
            assert await session.scalar(select(func.sum(CustomerOtpCode.attempts_count))) == MAX_ATTEMPTS
            assert old.attempts_count + new.attempts_count == 1
            assert sum(row.last_attempt_at is not None for row in [old, new]) == 1
            assert sum(row.verified_at is not None for row in [old, new]) == (1 if code == CODE else 0)
        if code == WRONG_CODE:
            assert_error(first_outcome, 400, INVALID)
        else:
            assert isinstance(first_outcome, OtpVerifyResult)
            assert first_outcome.access_token
            assert first_outcome.attempts_left_today == 0
        assert_error(second_outcome, 429, LIMIT)
    finally:
        generation_inserted.set()
        release_phone_lock_holder.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
