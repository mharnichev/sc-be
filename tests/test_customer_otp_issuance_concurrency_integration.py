"""Real OTP issuance races with isolated PostgreSQL and an in-memory sender."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.security import hash_otp_code
from app.models.customer_otp_code import CustomerOtpCode
from app.services import customer_auth as auth_module
from app.services.customer_auth import CustomerAuthService, OtpRequestResult
from app.services.sms_queue import SmsQueuePending, sms_request_context, use_sms_context
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


AT = datetime(2026, 10, 9, 12, tzinfo=UTC)
PHONE = "+380500001234"
FORMATTED_PHONE = "+38 (050) 000-12-34"
OTHER_PHONE = "+380500005678"
CODE = "123456"
MAX_SENDS = 3
RESEND_SECONDS = 120
TTL_MINUTES = 10
DAILY_LIMIT = "Daily OTP send limit reached for this phone number"
RECENT_PREFIX = "OTP already sent recently. Retry in "


class FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT.astimezone(tz) if tz is not None else AT.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def deterministic_otp_settings_and_no_external_sender(monkeypatch):
    monkeypatch.setattr(auth_module, "datetime", FrozenClock)
    monkeypatch.setattr(auth_module, "generate_otp_code", lambda: CODE)
    monkeypatch.setattr(settings, "otp_max_sends_per_day", MAX_SENDS)
    monkeypatch.setattr(settings, "otp_resend_interval_seconds", RESEND_SECONDS)
    monkeypatch.setattr(settings, "otp_code_ttl_minutes", TTL_MINUTES)
    monkeypatch.setattr(settings, "app_env", "local")

    async def forbidden_send(*args, **kwargs):
        raise AssertionError("Only the explicit in-memory sender may be used")

    monkeypatch.setattr("app.services.sms.SmsService.send_otp_code", forbidden_send)
    monkeypatch.setattr("app.services.sms.SmsService.send_message", forbidden_send)


async def seed_codes(database, count, *, phone=PHONE, recent=False, state="active", yesterday=False):
    async with database() as session:
        records = []
        for index in range(count):
            sent_at = AT - timedelta(seconds=(30 if recent else RESEND_SECONDS + 30) + index)
            if yesterday:
                sent_at -= timedelta(days=1)
            record = CustomerOtpCode(
                phone=phone, code_hash=hash_otp_code(phone, CODE), sent_at=sent_at,
                expires_at=AT - timedelta(seconds=1) if state == "expired" else AT + timedelta(minutes=TTL_MINUTES),
                verified_at=AT - timedelta(seconds=5) if state == "used" else None,
            )
            session.add(record)
            records.append(record)
        await session.commit()
        return [record.id for record in records]


class CommittedOtpSender:
    """Observe committed metadata and released locks before simulating delivery."""

    def __init__(self, database, session, issuer_pid, *, failure=None):
        self.database = database
        self.session = session
        self.issuer_pid = issuer_pid
        self.failure = failure
        self.calls = []

    async def send_otp_code(self, phone, code):
        assert not self.session.in_transaction(), "SMS must begin after the OTP commit"
        context = sms_request_context.get()
        assert context is not None
        assert context.priority == 0
        assert context.idempotency_key.startswith("otp:")
        otp_id = int(context.idempotency_key.removeprefix("otp:"))

        async def prove_commit(observer):
            observer_pid = await observer.scalar(select(func.pg_backend_pid()))
            assert observer_pid != self.issuer_pid
            record = await observer.get(CustomerOtpCode, otp_id)
            assert record is not None, "A separate backend must see the committed OTP"
            assert record.phone == phone
            assert record.code_hash == hash_otp_code(phone, code)
            assert record.sent_at == AT
            assert record.expires_at == AT + timedelta(minutes=TTL_MINUTES)
            assert record.attempts_count == 0
            assert record.verified_at is None
            assert not await observer.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                "WHERE pid = :issuer AND locktype = 'advisory' AND granted)"
            ), {"issuer": self.issuer_pid}), "Provider I/O must not hold the issuer's phone lock"

        async with self.database() as observer:
            observer_pid = await observer.scalar(select(func.pg_backend_pid()))
            if observer_pid == self.issuer_pid:
                # Commit can return the issuer's connection to the pool. Keep
                # that connection occupied while a distinct backend observes.
                async with self.database() as separate_observer:
                    await prove_commit(separate_observer)
            else:
                await prove_commit(observer)
        self.calls.append((phone, code, otp_id, context.priority, context.idempotency_key))
        if self.failure is not None:
            raise self.failure


def assert_success(result, *, sends_left):
    assert isinstance(result, OtpRequestResult)
    assert result.expires_in_seconds == TTL_MINUTES * 60
    assert result.retry_after_seconds == RESEND_SECONDS
    assert result.sends_left_today == sends_left
    assert result.debug_otp_code == CODE


def assert_error(error, detail):
    assert isinstance(error, HTTPException)
    assert (error.status_code, error.detail) == (429, detail)


async def single_request(database, phone=PHONE, *, failure=None):
    async with database() as session:
        pid = await session.scalar(select(func.pg_backend_pid()))
        sender = CommittedOtpSender(database, session, pid, failure=failure)
        service = CustomerAuthService()
        service.sms_service = sender
        try:
            outcome = await service.request_otp(session, phone)
        except (HTTPException, RuntimeError) as error:
            outcome = error
        assert await session.scalar(select(1)) == 1
        return outcome, sender


class IssuanceRace:
    def __init__(self, database, *, same_phone):
        self.database = database
        self.same_phone = same_phone
        self.ready = asyncio.Barrier(2)
        self.latest_barrier = asyncio.Barrier(2)
        self.first_latest = asyncio.Event()
        self.release_first = asyncio.Event()
        self.pids = {}
        self.counts = {}
        self.latest_ids = {}
        self.phone_lock_started = []
        self.first_holder = None
        self.blocked_pair = None
        self.senders = {}

    async def attempt(self, label, phone, monkeypatch):
        async with self.database() as session:
            execute = session.execute
            scalar = session.scalar
            self.pids[label] = (await execute(select(func.pg_backend_pid()))).scalar_one()
            service = CustomerAuthService()
            sender = CommittedOtpSender(self.database, session, self.pids[label])
            self.senders[label] = sender
            service.sms_service = sender
            latest = service._latest_otp
            sent_count = service._sent_count_today

            async def observe_sql(statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement).lower():
                    self.phone_lock_started.append(label)
                return await execute(statement, *args, **kwargs)

            async def observe_scalar(statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement).lower():
                    self.phone_lock_started.append(label)
                return await scalar(statement, *args, **kwargs)

            async def observe_count(*args, **kwargs):
                count = await sent_count(*args, **kwargs)
                self.counts[label] = count
                return count

            async def hold_latest(*args, **kwargs):
                row = await latest(*args, **kwargs)
                self.latest_ids[label] = row.id if row is not None else None
                if self.same_phone and self.phone_lock_started:
                    if self.first_holder is None:
                        self.first_holder = label
                        self.first_latest.set()
                        await asyncio.wait_for(self.release_first.wait(), timeout=10)
                else:
                    # Both count and latest result must precede either insert.
                    await asyncio.wait_for(self.latest_barrier.wait(), timeout=10)
                return row

            monkeypatch.setattr(session, "execute", observe_sql)
            monkeypatch.setattr(session, "scalar", observe_scalar)
            monkeypatch.setattr(service, "_sent_count_today", observe_count)
            monkeypatch.setattr(service, "_latest_otp", hold_latest)
            await asyncio.wait_for(self.ready.wait(), timeout=10)
            try:
                outcome = await service.request_otp(session, phone)
            except HTTPException as error:
                outcome = error
            assert await session.scalar(select(1)) == 1
            return outcome

    async def observe_lock(self):
        await asyncio.wait_for(self.first_latest.wait(), timeout=10)
        holder_pid = self.pids[self.first_holder]
        waiter_pid = next(pid for label, pid in self.pids.items() if label != self.first_holder)
        assert holder_pid != waiter_pid
        async with self.database() as observer:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                blocked = await observer.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE pid = :waiter AND wait_event_type = 'Lock' "
                    "AND wait_event = 'advisory' AND :holder = ANY(pg_blocking_pids(pid)))"
                ), {"waiter": waiter_pid, "holder": holder_pid})
                if blocked:
                    assert len(self.phone_lock_started) == 2
                    assert len(self.latest_ids) == 1
                    self.blocked_pair = (holder_pid, waiter_pid)
                    self.release_first.set()
                    return
                await asyncio.sleep(0.01)
        raise AssertionError("Second issuer did not block on the first phone transaction lock")


async def concurrent_requests(database, monkeypatch, phones):
    same_phone = CustomerAuthService().normalize_phone(phones[0]) == CustomerAuthService().normalize_phone(phones[1])
    race = IssuanceRace(database, same_phone=same_phone)
    # Determine the branch from actual SQL calls, without assuming a production
    # lock exists: an observer is only needed once the first locked reader waits.
    async def observe_if_locked():
        while not race.first_latest.is_set():
            if all(task.done() for task in request_tasks):
                return
            await asyncio.sleep(0.01)
        await race.observe_lock()

    request_tasks = [asyncio.create_task(race.attempt(label, phone, monkeypatch)) for label, phone in zip(["first", "second"], phones)]
    observer = asyncio.create_task(observe_if_locked())
    tasks = [*request_tasks, observer]
    try:
        values = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
        assert len(race.pids) == len(set(race.pids.values())) == 2
        if same_phone and race.phone_lock_started:
            assert race.blocked_pair is not None
        else:
            assert len(race.latest_ids) == 2
            assert race.latest_barrier.n_waiting == 0
        return values[:2], race
    finally:
        race.release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("second_phone", [PHONE, FORMATTED_PHONE], ids=["same-phone", "normalized-phone"])
async def test_cold_concurrent_issuance_respects_cooldown(database, monkeypatch, second_phone):
    outcomes, race = await concurrent_requests(database, monkeypatch, [PHONE, second_phone])
    async with database() as session:
        records = list(await session.scalars(select(CustomerOtpCode)))
        assert len(records) == 1
        assert records[0].phone == PHONE
    successes = [value for value in outcomes if isinstance(value, OtpRequestResult)]
    errors = [value for value in outcomes if isinstance(value, HTTPException)]
    assert len(successes) == len(errors) == 1
    assert_success(successes[0], sends_left=MAX_SENDS - 1)
    assert_error(errors[0], f"{RECENT_PREFIX}{RESEND_SECONDS} seconds")
    assert sum(len(sender.calls) for sender in race.senders.values()) == 1
    if race.phone_lock_started:
        assert sorted(race.counts.values()) == [0, 1]
    else:
        assert list(race.counts.values()) == [0, 0]
        assert all(value is None for value in race.latest_ids.values())


@pytest.mark.anyio
@pytest.mark.parametrize("second_phone", [PHONE, FORMATTED_PHONE], ids=["same-phone", "normalized-phone"])
async def test_concurrent_issuance_cannot_exceed_one_remaining_daily_send(database, monkeypatch, second_phone):
    old_ids = await seed_codes(database, MAX_SENDS - 1)
    outcomes, race = await concurrent_requests(database, monkeypatch, [PHONE, second_phone])
    async with database() as session:
        records = list(await session.scalars(select(CustomerOtpCode)))
        assert len(records) == MAX_SENDS
        assert set(old_ids).issubset({row.id for row in records})
        assert sum(row.sent_at == AT for row in records) == 1
    successes = [value for value in outcomes if isinstance(value, OtpRequestResult)]
    errors = [value for value in outcomes if isinstance(value, HTTPException)]
    assert len(successes) == len(errors) == 1
    assert_success(successes[0], sends_left=0)
    assert_error(errors[0], DAILY_LIMIT)
    assert sum(len(sender.calls) for sender in race.senders.values()) == 1
    if race.phone_lock_started:
        assert sorted(race.counts.values()) == [MAX_SENDS - 1, MAX_SENDS]
    else:
        assert list(race.counts.values()) == [MAX_SENDS - 1, MAX_SENDS - 1]
        assert set(race.latest_ids.values()) == {old_ids[0]}


@pytest.mark.anyio
async def test_independent_phones_can_issue_concurrently(database, monkeypatch):
    outcomes, race = await concurrent_requests(database, monkeypatch, [PHONE, OTHER_PHONE])
    for outcome in outcomes:
        assert_success(outcome, sends_left=MAX_SENDS - 1)
    assert race.blocked_pair is None
    assert len(race.latest_ids) == 2
    assert all(value is None for value in race.latest_ids.values())
    assert list(race.counts.values()) == [0, 0]
    assert all(len(sender.calls) == 1 for sender in race.senders.values())
    async with database() as session:
        assert set(await session.scalars(select(CustomerOtpCode.phone))) == {PHONE, OTHER_PHONE}


@pytest.mark.anyio
@pytest.mark.parametrize("recent", [False, True], ids=["elapsed-cooldown", "active-cooldown"])
async def test_daily_limit_precedes_cooldown_and_never_sends(database, recent):
    ids = await seed_codes(database, MAX_SENDS, recent=recent)
    outcome, sender = await single_request(database)
    assert_error(outcome, DAILY_LIMIT)
    assert sender.calls == []
    async with database() as session:
        assert set(await session.scalars(select(CustomerOtpCode.id))) == set(ids)


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["active", "used", "expired"])
async def test_recent_latest_code_enforces_cooldown_regardless_of_usage_or_expiry(database, state):
    ids = await seed_codes(database, 1, recent=True, state=state)
    outcome, sender = await single_request(database)
    assert_error(outcome, f"{RECENT_PREFIX}{RESEND_SECONDS - 30} seconds")
    assert sender.calls == []
    async with database() as session:
        assert list(await session.scalars(select(CustomerOtpCode.id))) == ids


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["active", "used", "expired"])
async def test_elapsed_cooldown_allows_new_code_without_replacing_old_metadata(database, state):
    ids = await seed_codes(database, 1, state=state)
    outcome, sender = await single_request(database)
    assert_success(outcome, sends_left=MAX_SENDS - 2)
    assert len(sender.calls) == 1
    async with database() as session:
        records = list(await session.scalars(select(CustomerOtpCode).order_by(CustomerOtpCode.id)))
        assert len(records) == 2
        old = await session.get(CustomerOtpCode, ids[0])
        assert old.attempts_count == 0
        assert (old.verified_at is not None) == (state == "used")
        assert old.sent_at == AT - timedelta(seconds=RESEND_SECONDS + 30)


@pytest.mark.anyio
async def test_previous_utc_day_sends_do_not_use_todays_budget(database):
    ids = await seed_codes(database, MAX_SENDS, yesterday=True)
    outcome, sender = await single_request(database)
    assert_success(outcome, sends_left=MAX_SENDS - 1)
    assert len(sender.calls) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerOtpCode)) == MAX_SENDS + 1
        assert set(ids).issubset(set(await session.scalars(select(CustomerOtpCode.id))))


@pytest.mark.anyio
async def test_legacy_local_phone_retains_separate_exact_normalized_issuance_bucket(database):
    await seed_codes(database, MAX_SENDS, phone="+0500001234", recent=True)
    outcome, sender = await single_request(database, FORMATTED_PHONE)
    assert_success(outcome, sends_left=MAX_SENDS - 1)
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == PHONE


@pytest.mark.anyio
async def test_invalid_phone_keeps_existing_error_without_record_or_notification(database):
    outcome, sender = await single_request(database, "invalid")
    assert isinstance(outcome, HTTPException)
    assert (outcome.status_code, outcome.detail) == (422, "Invalid phone number")
    assert sender.calls == []
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerOtpCode)) == 0


@pytest.mark.anyio
async def test_production_response_keeps_debug_otp_hidden(database, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    outcome, sender = await single_request(database)
    assert isinstance(outcome, OtpRequestResult)
    assert outcome.debug_otp_code is None
    assert outcome.expires_in_seconds == TTL_MINUTES * 60
    assert len(sender.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("delivery", ["accepted", "pending", "failed"])
async def test_sender_pending_or_failure_preserves_committed_otp_and_sms_context(database, delivery):
    pending = SmsQueuePending(SimpleNamespace(id="synthetic-job", error_code=None, error_detail=None, status="queued"))
    failure = pending if delivery == "pending" else RuntimeError("synthetic provider failure") if delivery == "failed" else None
    with use_sms_context(priority=17, idempotency_key="outer-context") as _:
        outer_context = sms_request_context.get()
        outcome, sender = await single_request(database, failure=failure)
        assert sms_request_context.get() is outer_context
    if delivery == "failed":
        assert outcome is failure
    else:
        assert_success(outcome, sends_left=MAX_SENDS - 1)
    assert len(sender.calls) == 1
    phone, code, otp_id, priority, key = sender.calls[0]
    assert (phone, code, priority, key) == (PHONE, CODE, 0, f"otp:{otp_id}")
    async with database() as session:
        records = list(await session.scalars(select(CustomerOtpCode)))
        assert len(records) == 1
        assert records[0].id == otp_id
        assert records[0].verified_at is None


@pytest.mark.anyio
async def test_rejected_commit_does_not_call_sender_or_leave_otp(database, monkeypatch):
    async with database() as session:
        pid = await session.scalar(select(func.pg_backend_pid()))
        sender = CommittedOtpSender(database, session, pid)
        service = CustomerAuthService()
        service.sms_service = sender

        async def reject_commit():
            raise RuntimeError("synthetic commit rejection")

        monkeypatch.setattr(session, "commit", reject_commit)
        with pytest.raises(RuntimeError, match="synthetic commit rejection"):
            await service.request_otp(session, PHONE)
        assert sender.calls == []
        await session.rollback()
        assert await session.scalar(select(1)) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerOtpCode)) == 0
        assert not await session.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks "
            "WHERE pid = :issuer AND locktype = 'advisory' AND granted)"
        ), {"issuer": pid})
