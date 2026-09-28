from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models.booking_funnel import BookingFunnelEvent as Event, BookingFunnelEventType as Type
from app.services.booking_no_slots import BookingNoSlotsService


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE masters (id INTEGER PRIMARY KEY, full_name TEXT, last_name TEXT)'))
        conn.execute(text('CREATE TABLE barber_services (id INTEGER PRIMARY KEY, title_uk TEXT, name TEXT)'))
        Event.__table__.create(conn)
        conn.execute(text("INSERT INTO masters VALUES (7, 'Андрій', 'Віканов'), (8, 'Інший', NULL)"))
        conn.execute(text("INSERT INTO barber_services VALUES (11, 'Стрижка', 'Cut')"))
    with Session(engine) as sync:
        class AsyncAdapter:
            async def execute(self, statement):
                return sync.execute(statement)
        yield sync, AsyncAdapter()


START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 29, tzinfo=timezone.utc)


def add(sync, i, *, session='s1', master=7, at=None, target=None, kind=Type.no_slot):
    sync.add(Event(id=i, event_id_hash=f'event{i}', event_type=kind, source='client',
        anonymous_session_hash=session, master_id=master, service_id=11, service_ids_key='11',
        duration_minutes=60, target_date=target or date(2026, 10, 1), occurred_at=at or START + timedelta(seconds=i)))
    sync.flush()


@pytest.mark.anyio
async def test_september_summary_deduplicates_full_period_and_preserves_checks(db):
    sync, session = db
    for i in range(58):
        add(sync, i + 1, session=f's{i % 3}', target=date(2026, 10, 1) + timedelta(days=i % 52))
    service = BookingNoSlotsService()
    summary = (await service.summaries(session, start=START, end=END))[0]
    assert (summary.unique_sessions, summary.observations, summary.contexts) == (3, 58, 52)
    first = await service.details(session, start=START, end=END, master_id=7, limit=2)
    second = await service.details(session, start=START, end=END, master_id=7, limit=2, offset=2, snapshot_id=first.snapshot_id)
    attempts = first.items + second.items
    assert first.has_more and not second.has_more
    assert len({a.attempt_id for a in attempts}) == 3
    assert sum(a.observations for a in attempts) == summary.observations
    attempt = attempts[0]
    checks = await service.details(session, start=START, end=END, master_id=7, attempt_id=attempt.attempt_id, limit=5, snapshot_id=first.snapshot_id)
    assert checks.total == attempt.observations and checks.has_more
    assert 'anonymous_session_hash' not in first.model_dump_json()
    assert all(a.attempt_id not in ('s0', 's1', 's2') for a in attempts)


@pytest.mark.anyio
async def test_cap_filtering_cross_master_nonadditivity(db):
    sync, session = db
    for i in range(300):
        add(sync, i + 1, target=date(2026, 10, 1) + timedelta(days=i))
    add(sync, 301, master=8)
    add(sync, 302, at=END)
    add(sync, 303, at=START - timedelta(seconds=1))
    add(sync, 304, session=None)
    service = BookingNoSlotsService()
    all_rows = await service.summaries(session, start=START, end=END)
    assert [r.unique_sessions for r in all_rows] == [1, 1]
    summary = (await service.summaries(session, start=START, end=END, master_id=7))[0]
    assert summary.observations == 301 and summary.contexts == 300
    assert summary.unattributed_observations == 1
    details = await service.details(session, start=START, end=END, master_id=7)
    assert details.total == 1 and details.items[0].observations == 300


@pytest.mark.anyio
async def test_snapshot_outcomes_and_neutral_rapid_signal(db):
    sync, session = db
    add(sync, 1)
    add(sync, 2, at=START + timedelta(seconds=5))
    add(sync, 3, kind=Type.booking_success, at=START - timedelta(seconds=1))
    add(sync, 4, master=8, kind=Type.booking_success, at=END + timedelta(days=1))
    add(sync, 5, kind=Type.slot_selected, at=START + timedelta(seconds=30))
    service = BookingNoSlotsService()
    first = await service.details(session, start=START, end=END, master_id=7)
    attempt = first.items[0]
    assert attempt.rapid_checks == 1
    assert attempt.later_time_selection and attempt.later_booking_other_master
    assert not attempt.later_booking_same_master
    add(sync, 6, kind=Type.booking_success, at=END + timedelta(days=2))
    add(sync, 7, session='new')
    frozen = await service.details(session, start=START, end=END, master_id=7, snapshot_id=first.snapshot_id)
    assert frozen.total == 1 and not frozen.items[0].later_booking_same_master
    live = await service.details(session, start=START, end=END, master_id=7)
    assert live.total == 2
    assert any(a.later_booking_same_master for a in live.items)
    with pytest.raises(Exception) as error:
        await service.details(session, start=START, end=END, master_id=8, attempt_id=attempt.attempt_id)
    assert error.value.status_code == 404


@pytest.mark.anyio
async def test_summary_snapshot_excludes_new_checks_in_same_session(db):
    sync, session = db
    add(sync, 1)
    service = BookingNoSlotsService()
    snapshot = await service.snapshot_id(session)
    add(sync, 2)  # Arrives even before the summary queries execute.
    summary = (await service.summaries(session, start=START, end=END, snapshot_id=snapshot))[0]
    detail = await service.details(session, start=START, end=END, master_id=7, snapshot_id=snapshot)
    assert summary.observations == detail.items[0].observations == 1
    assert summary.contexts == detail.items[0].contexts == 1
    assert summary.unique_sessions == detail.total == 1
    assert (await service.summaries(session, start=START, end=END))[0].observations == 2


@pytest.mark.anyio
async def test_unknown_master_and_date_preserve_historical_evidence(db):
    sync, session = db
    add(sync, 1, master=None)
    event = sync.get(Event, 1)
    event.target_date = None
    sync.flush()
    service = BookingNoSlotsService()
    summary = (await service.summaries(session, start=START, end=END))[0]
    assert summary.master_id is None and summary.master_name is None
    assert summary.checked_dates == 0 and summary.date_from is None and summary.date_to is None
    assert summary.contexts == summary.observations == summary.unique_sessions == 1
    page = await service.details(session, start=START, end=END, unknown_master=True)
    assert page.items[0].checked_dates == 0 and page.items[0].date_from is None
    checks = await service.details(session, start=START, end=END, unknown_master=True,
        attempt_id=page.items[0].attempt_id, snapshot_id=page.snapshot_id)
    assert checks.items[0].target_date is None


@pytest.mark.anyio
async def test_detail_route_passes_inclusive_kyiv_dst_bounds(monkeypatch):
    from types import SimpleNamespace
    from app.api.v1.routes.statistics import get_admin_booking_no_slots
    captured = {}
    async def details(self, session, **kwargs):
        captured.update(kwargs)
        return 'result'
    monkeypatch.setattr(BookingNoSlotsService, 'details', details)
    result = await get_admin_booking_no_slots(
        date_from=date(2026, 3, 28), date_to=date(2026, 3, 29), master_id=7,
        unknown_master=False, unattributed=False, attempt_id=None, offset=0, limit=20, snapshot_id=42,
        current_user=SimpleNamespace(is_superuser=True), session=None,
    )
    assert result == 'result' and captured['snapshot_id'] == 42
    assert captured['start'].astimezone(timezone.utc) == datetime(2026, 3, 27, 22, tzinfo=timezone.utc)
    assert captured['end'].astimezone(timezone.utc) == datetime(2026, 3, 29, 21, tzinfo=timezone.utc)
    assert captured['master_id'] == 7


@pytest.mark.anyio
async def test_unattributed_checks_are_paginated_without_inventing_sessions(db):
    sync, session = db
    for i in range(1, 4):
        add(sync, i, session=None, master=None)
    service = BookingNoSlotsService()
    snapshot = await service.snapshot_id(session)
    summary = (await service.summaries(session, start=START, end=END, snapshot_id=snapshot))[0]
    assert summary.unique_sessions == 0 and summary.unattributed_observations == 3
    attempts = await service.details(session, start=START, end=END, unknown_master=True, snapshot_id=snapshot)
    assert attempts.total == 0
    first = await service.details(session, start=START, end=END, unknown_master=True,
        unattributed=True, limit=2, snapshot_id=snapshot)
    add(sync, 4, session=None, master=None)
    second = await service.details(session, start=START, end=END, unknown_master=True,
        unattributed=True, offset=2, limit=2, snapshot_id=snapshot)
    assert first.total == second.total == summary.unattributed_observations
    assert first.has_more and not second.has_more
    assert len(first.items + second.items) == 3
    assert all(not hasattr(item, 'attempt_id') for item in first.items)
    with pytest.raises(Exception) as error:
        await service.details(session, start=START, end=END, unknown_master=True,
            unattributed=True, attempt_id='a' * 64)
    assert error.value.status_code == 422
