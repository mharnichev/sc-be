"""Session-set contract oracle for funnel aggregation on synthetic PostgreSQL."""
from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.booking import Master
from app.models.booking_funnel import BookingFunnelEvent, BookingFunnelEventSource, BookingFunnelEventType as Event
from app.schemas.booking_funnel import FUNNEL_STEP_TYPES, BookingFunnelNoSlotOutcomes
from app.services.booking_funnel import BookingFunnelService, _no_slot_outcome_rows, _percent, build_funnel_aggregate
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401

pytestmark = pytest.mark.anyio
ANCHOR = datetime(2026, 10, 5, tzinfo=UTC)


async def seed_events(database):
    rng = random.Random(713)
    rows = []
    async with database() as session:
        masters = [Master(full_name=f"Synthetic funnel master {i}") for i in range(3)]
        session.add_all(masters)
        await session.flush()
        ids = [m.id for m in masters]
        for index in range(90):
            session_hash = hashlib.sha256(f"synthetic-session-{index}".encode()).hexdigest()
            start = ANCHOR - timedelta(days=rng.choice([0, 1, 6, 7, 30, 31, 365, 366, 400]))
            types = [Event.booking_start, *rng.choices(list(Event), k=12)]
            if index % 11 == 0:
                types = [kind for kind in types if kind != Event.booking_start]
            if index == 2:
                types += [Event.booking_error] * 1000
            for ordinal, kind in enumerate(types):
                at = start if ordinal == 0 else start + timedelta(days=rng.choice([0, 1, 30]), seconds=ordinal)
                # An earlier repeated start changes the entire cohort membership.
                if kind == Event.booking_start and index % 7 == 0:
                    at = start - timedelta(days=500)
                master_id = rng.choice([None, *ids])
                anon = None if index % 13 == 0 else session_hash
                rows.append(BookingFunnelEvent(
                    event_id_hash=hashlib.sha256(f"synthetic-event-{index}-{ordinal}".encode()).hexdigest(),
                    event_type=kind, source=BookingFunnelEventSource.server if kind == Event.booking_success else BookingFunnelEventSource.client,
                    anonymous_session_hash=anon, master_id=master_id, occurred_at=at,
                    target_date=(ANCHOR + timedelta(days=index % 3)).date() if kind == Event.no_slot and ordinal % 2 else None,
                    duration_minutes=30 if kind == Event.no_slot else None,
                ))
        session.add_all(rows)
        await session.commit()
        return ids


def oracle(rows, start, end, master_id, thresholds):
    first_start = {}
    for row in rows:
        if row.event_type == Event.booking_start and row.anonymous_session_hash is not None:
            key = row.anonymous_session_hash
            first_start[key] = min(first_start.get(key, row.occurred_at), row.occurred_at)
    cohort = {key for key, at in first_start.items() if start <= at < end}
    cohort_rows = [row for row in rows if row.anonymous_session_hash in cohort]
    scoped = {row.anonymous_session_hash for row in cohort_rows if row.master_id == master_id}
    steps = {kind: set() for kind in FUNNEL_STEP_TYPES}
    for row in cohort_rows:
        if row.event_type not in steps:
            continue
        matches = (master_id is None or row.master_id == master_id or (
            row.event_type in (Event.booking_start, Event.service_selected)
            and row.master_id is None and row.anonymous_session_hash in scoped
        ))
        if matches:
            steps[row.event_type].add(row.anonymous_session_hash)
    operational = {kind: set() for kind in (Event.master_selected, Event.no_slot, Event.stale_schedule, Event.booking_error)}
    unattributed = set()
    for row in rows:
        if not start <= row.occurred_at < end or (master_id is not None and row.master_id != master_id):
            continue
        if row.event_type == Event.booking_success and row.anonymous_session_hash is None:
            unattributed.add(row.id)
        if row.event_type in operational and row.anonymous_session_hash is not None:
            operational[row.event_type].add(row.anonymous_session_hash)
    counts = {kind: len(steps.get(kind, operational.get(kind, set()))) for kind in Event}
    transitions = list(zip(FUNNEL_STEP_TYPES, FUNNEL_STEP_TYPES[1:]))
    now = datetime.now(UTC)
    affected = {}
    for row in rows:
        if (row.event_type == Event.no_slot and row.anonymous_session_hash is not None
                and start <= row.occurred_at < end and row.occurred_at <= now
                and (master_id is None or row.master_id == master_id)):
            affected.setdefault(row.anonymous_session_hash, []).append(row.occurred_at)
    outcomes = {"booked": 0, "selected": 0, "unresolved": 0, "pending": 0, "eligible": 0}
    for key, check_times in affected.items():
        first, last = min(check_times), max(check_times)
        last = max([last] + [row.occurred_at for row in rows
                             if row.anonymous_session_hash == key and row.event_type == Event.no_slot
                             and first < row.occurred_at <= now])
        continuation = [row.event_type for row in rows
                        if row.anonymous_session_hash == key and first < row.occurred_at <= now]
        if Event.booking_success in continuation:
            outcomes["booked"] += 1
        elif Event.slot_selected in continuation:
            outcomes["selected"] += 1
        elif last <= now - timedelta(hours=2):
            outcomes["unresolved"] += 1
            if key in operational[Event.master_selected]:
                outcomes["eligible"] += 1
        else:
            outcomes["pending"] += 1
    denominator = len(operational[Event.master_selected])
    return build_funnel_aggregate(
        counts, thresholds=thresholds, unattributed_booking_successes=len(unattributed),
        transition_counts={pair: len(steps[pair[0]] & steps[pair[1]]) for pair in transitions},
        tracking_gaps={pair: len(steps[pair[1]] - steps[pair[0]]) for pair in transitions},
        overall_success_sessions=len(steps[Event.booking_start] & steps[Event.booking_success]),
        no_slot_rate_sessions=len(operational[Event.master_selected] & operational[Event.no_slot]),
        no_slot_denominator_sessions=denominator,
        no_slot_outcomes=BookingFunnelNoSlotOutcomes(
            status="available",
            affected_sessions=len(affected), booked_sessions=outcomes["booked"],
            time_selected_sessions=outcomes["selected"],
            unresolved_sessions=outcomes["unresolved"], pending_sessions=outcomes["pending"],
            denominator_sessions=denominator, unresolved_rate_sessions=outcomes["eligible"],
            unresolved_rate_percent=_percent(outcomes["eligible"], denominator),
        ),
        stale_schedule_rate_sessions=len(operational[Event.master_selected] & operational[Event.stale_schedule]),
        booking_error_rate_sessions=len(operational[Event.master_selected] & operational[Event.booking_error]),
    )


@pytest.mark.parametrize("days", [7, 31, 366])
@pytest.mark.parametrize("scope", [None, 0, 1, 2, "missing"])
async def test_funnel_metrics_equal_independent_session_set_oracle(database, days, scope):
    ids = await seed_events(database)
    master_id = None if scope is None else max(ids) + 100 if scope == "missing" else ids[scope]
    start = ANCHOR - timedelta(days=days)
    service = BookingFunnelService()
    async with database() as session:
        rows = (await session.scalars(select(BookingFunnelEvent))).all()
        expected = oracle(rows, start, ANCHOR, master_id, service.thresholds)
        actual = await service.aggregate(session, start=start, end=ANCHOR, master_id=master_id, include_latest_digest=False)
    excluded = {"no_slot_dates", "no_slot_contexts", "no_slot_contexts_truncated", "no_slot_unknown_date_count", "no_slot_snapshot_id", "no_slot_masters"}
    assert actual.model_dump(mode="json", exclude=excluded) == expected.model_dump(mode="json", exclude=excluded)


@pytest.mark.parametrize("master_id", [None, 1])
async def test_empty_postgresql_funnel_returns_empty_aggregate(database, master_id):
    async with database() as session:
        actual = await BookingFunnelService().aggregate(
            session, start=ANCHOR-timedelta(days=7), end=ANCHOR,
            master_id=master_id, include_latest_digest=False,
        )
    assert actual.status == "empty"
    assert actual.steps == actual.step_to_step_conversion == actual.operational_alerts == []
    assert actual.tracking_gap_count == actual.unattributed_booking_successes == 0
    assert actual.no_slot_dates == actual.no_slot_contexts == actual.no_slot_masters == []
    assert actual.no_slot_snapshot_id == 0
    assert actual.no_slot_outcomes is not None
    assert actual.no_slot_outcomes.status == "available"
    assert actual.no_slot_outcomes.unresolved_rate_percent is None


async def test_no_slot_outcomes_follow_first_check_across_periods_and_masters(database):
    start, end = ANCHOR - timedelta(days=1), ANCHOR
    now = end + timedelta(hours=3)
    async with database() as session:
        first_master = Master(full_name="First outcome master")
        other_master = Master(full_name="Other outcome master")
        session.add_all([first_master, other_master])
        await session.flush()
        first_id, other_id = first_master.id, other_master.id
        rows = []

        def add(key, kind, at, master):
            rows.append(BookingFunnelEvent(
                event_id_hash=hashlib.sha256(f"outcome-{key}-{kind.value}-{len(rows)}".encode()).hexdigest(),
                event_type=kind,
                source=BookingFunnelEventSource.server if kind == Event.booking_success else BookingFunnelEventSource.client,
                anonymous_session_hash=hashlib.sha256(key.encode()).hexdigest(),
                master_id=master,
                occurred_at=at,
            ))

        old = end - timedelta(hours=12)
        for key in ("booked", "selected", "unresolved", "pending", "boundary", "no-baseline", "before-only"):
            if key != "no-baseline":
                add(key, Event.master_selected, old - timedelta(minutes=1), first_id)
            add(key, Event.no_slot, old, first_id)
        add("booked", Event.slot_selected, end + timedelta(minutes=10), other_id)
        add("booked", Event.booking_success, end + timedelta(minutes=20), other_id)
        add("selected", Event.slot_selected, end + timedelta(minutes=10), other_id)
        add("unresolved", Event.no_slot, old + timedelta(minutes=5), first_id)
        add("pending", Event.no_slot, now - timedelta(hours=2) + timedelta(microseconds=1), first_id)
        add("boundary", Event.no_slot, now - timedelta(hours=2), first_id)
        add("before-only", Event.booking_success, old - timedelta(minutes=2), first_id)
        session.add_all(rows)
        await session.commit()

    async with database() as session:
        actual = await _no_slot_outcome_rows(
            session, start=start, end=end, master_id=first_id, now=now,
        )
    assert tuple(actual) == (7, 1, 1, 4, 1, 3)


async def test_no_slot_recovery_at_first_check_is_not_later(database):
    start, end = ANCHOR - timedelta(days=1), ANCHOR
    first = end - timedelta(hours=5)
    key = hashlib.sha256(b"same-time").hexdigest()
    async with database() as session:
        master = Master(full_name="Same time outcome master")
        session.add(master)
        await session.flush()
        session.add_all([
            BookingFunnelEvent(
                event_id_hash=hashlib.sha256(f"same-{index}".encode()).hexdigest(),
                event_type=kind,
                source=BookingFunnelEventSource.server if kind == Event.booking_success else BookingFunnelEventSource.client,
                anonymous_session_hash=key, master_id=master.id, occurred_at=first,
            ) for index, kind in enumerate((Event.master_selected, Event.no_slot, Event.booking_success))
        ])
        await session.commit()
        master_id = master.id
    async with database() as session:
        actual = await _no_slot_outcome_rows(
            session, start=start, end=end, master_id=master_id, now=end,
        )
    assert tuple(actual) == (1, 0, 0, 1, 0, 1)


async def test_orphan_no_slot_recovery_is_available_without_rate_baseline(database):
    start, end = ANCHOR - timedelta(days=1), ANCHOR
    key = hashlib.sha256(b"orphan-recovered-session").hexdigest()
    async with database() as session:
        checked_master = Master(full_name="Checked orphan master")
        booked_master = Master(full_name="Booked orphan master")
        session.add_all([checked_master, booked_master])
        await session.flush()
        session.add_all([
            BookingFunnelEvent(
                event_id_hash=hashlib.sha256(b"orphan-no-slot").hexdigest(),
                event_type=Event.no_slot, source=BookingFunnelEventSource.client,
                anonymous_session_hash=key, master_id=checked_master.id,
                occurred_at=end - timedelta(hours=1),
            ),
            BookingFunnelEvent(
                event_id_hash=hashlib.sha256(b"orphan-booking").hexdigest(),
                event_type=Event.booking_success, source=BookingFunnelEventSource.server,
                anonymous_session_hash=key, master_id=booked_master.id,
                occurred_at=end + timedelta(minutes=5),
            ),
        ])
        await session.commit()
        checked_master_id = checked_master.id

    async with database() as session:
        actual = await BookingFunnelService().aggregate(
            session, start=start, end=end, master_id=checked_master_id,
            include_latest_digest=False,
        )
    outcomes = actual.no_slot_outcomes
    assert outcomes is not None
    assert outcomes.status == "available"
    assert outcomes.affected_sessions == outcomes.booked_sessions == 1
    assert outcomes.time_selected_sessions == outcomes.unresolved_sessions == outcomes.pending_sessions == 0
    assert outcomes.denominator_sessions == outcomes.unresolved_rate_sessions == 0
    assert outcomes.unresolved_rate_percent is None
    assert actual.recommended_action is None
