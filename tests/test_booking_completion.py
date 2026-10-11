from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus
from app.models.messaging import Campaign, MessageChannel
from app.services import booking_completion as completion
from app.services.booking import KYIV_TZ
from app.services.messaging import MessagingService


@pytest.fixture
def anyio_backend():
    return "asyncio"


def at(day, hour=20, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=KYIV_TZ)


@pytest.mark.parametrize("now,expected", [
    (at(11, 19, 59), at(10)), (at(11), at(11)),
    (at(12, 0), at(11)), (at(12, 10), at(11)),
    (at(11).astimezone(UTC), at(11)),
])
def test_workday_cutoff_uses_kyiv_calendar(now, expected):
    assert completion.completion_bounds(now, date(2026, 10, 11)) == (at(11, 0), expected)


def test_cutoff_across_dst_uses_local_workday():
    now = datetime(2026, 10, 25, 9, tzinfo=KYIV_TZ)
    lower, cutoff = completion.completion_bounds(now, date(2026, 10, 11))
    assert lower == at(11, 0)
    assert cutoff == datetime(2026, 10, 24, 20, tzinfo=KYIV_TZ)
    assert cutoff.utcoffset() == timedelta(hours=3)


class AsyncOrmSession:
    """Actual SQLite persistence; scheduler collaborators never contact providers."""
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setattr(settings, "booking_auto_completion_enabled", True)
    monkeypatch.setattr(settings, "booking_auto_completion_starts_on", date(2026, 10, 11))
    promotion = SimpleNamespace(sync_first_visit_status=AsyncMock())
    repeat = SimpleNamespace(mark_repeat_visit_completed=AsyncMock())
    monkeypatch.setattr(completion, "PromotionService", lambda: promotion)
    monkeypatch.setattr(completion, "repeat_booking_service", repeat)
    engine = create_engine("sqlite://")
    for table in (BarberService.__table__, Booking.__table__, BookingServiceItem.__table__):
        table.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session, AsyncOrmSession(session), promotion, repeat
    engine.dispose()


def add_visit(session, identifier, end, status=BookingStatus.confirmed):
    session.add(Booking(
        id=identifier, master_id=1, service_id=1,
        customer_name="Sandbox", customer_phone="+380500000001",
        start_at=end - timedelta(hours=1), end_at=end, status=status,
    ))
    session.commit()


@pytest.mark.anyio
async def test_only_closed_confirmed_visits_complete_and_reruns_are_idempotent(database):
    session, adapter, promotion, repeat = database
    add_visit(session, 1, at(11))
    add_visit(session, 2, at(10))  # Historical records stay unchanged.
    add_visit(session, 3, at(12, 11))  # Future workday.
    for identifier, status in enumerate((BookingStatus.pending, BookingStatus.cancelled,
                                        BookingStatus.no_show, BookingStatus.completed), 4):
        add_visit(session, identifier, at(11, 12), status)
    assert await completion.complete_closed_workday_bookings(adapter, now=at(11, 19, 59)) == 0
    assert await completion.complete_closed_workday_bookings(adapter, now=at(11)) == 1
    session.expire_all()
    visit = session.get(Booking, 1)
    assert visit.status == BookingStatus.completed
    assert visit.completed_at == at(11).replace(tzinfo=None)
    assert session.get(Booking, 2).status == BookingStatus.confirmed
    assert session.get(Booking, 3).status == BookingStatus.confirmed
    assert [session.get(Booking, i).status for i in range(4, 8)] == [
        BookingStatus.pending, BookingStatus.cancelled, BookingStatus.no_show, BookingStatus.completed,
    ]
    promotion.sync_first_visit_status.assert_awaited_once()
    repeat.mark_repeat_visit_completed.assert_awaited_once()
    assert await completion.complete_closed_workday_bookings(adapter, now=at(12, 0)) == 0


@pytest.mark.anyio
async def test_downtime_recovery_and_failed_visit_rollback_do_not_block_others(database):
    session, adapter, promotion, repeat = database
    add_visit(session, 1, at(11, 12))
    add_visit(session, 2, at(12, 12))
    # Fail after the status has changed: the full visit must roll back.
    repeat.mark_repeat_visit_completed.side_effect = [RuntimeError("conflict"), None]
    assert await completion.complete_closed_workday_bookings(adapter, now=at(13, 10)) == 1
    session.expire_all()
    assert session.get(Booking, 1).status == BookingStatus.confirmed
    assert session.get(Booking, 1).completed_at is None
    assert session.get(Booking, 2).status == BookingStatus.completed
    repeat.mark_repeat_visit_completed.side_effect = None
    assert await completion.complete_closed_workday_bookings(adapter, now=at(13, 10)) == 1


@pytest.mark.anyio
async def test_cancellation_between_discovery_and_lock_is_respected(database):
    session, adapter, _, repeat = database
    add_visit(session, 1, at(11, 12))
    original_commit = adapter.commit
    first_commit = True

    async def cancel_after_discovery():
        nonlocal first_commit
        await original_commit()
        if first_commit:
            first_commit = False
            session.get(Booking, 1).status = BookingStatus.cancelled
            session.commit()

    adapter.commit = cancel_after_discovery
    assert await completion.complete_closed_workday_bookings(adapter, now=at(11)) == 0
    assert session.get(Booking, 1).status == BookingStatus.cancelled
    repeat.mark_repeat_visit_completed.assert_not_awaited()


@pytest.mark.anyio
async def test_disabled_worker_does_not_touch_database(monkeypatch):
    monkeypatch.setattr(settings, "booking_auto_completion_enabled", False)
    session = SimpleNamespace(execute=AsyncMock())
    assert await completion.complete_closed_workday_bookings(session, now=at(11)) == 0
    session.execute.assert_not_awaited()


@pytest.mark.parametrize("metadata", [
    {"quiet_hours_enabled": False},
    {"quiet_hours_enabled": True, "quiet_hours_from": "22:00", "quiet_hours_to": "08:00"},
])
def test_review_sms_night_guard_cannot_be_disabled(metadata):
    campaign = Campaign(metadata_json=metadata)
    for now in (at(11), at(11, 23, 59), at(12, 1), at(12, 9, 59)):
        assert MessagingService.review_sms_deferred_until(campaign, MessageChannel.sms, now=now) == at(12, 10)
    assert MessagingService.review_sms_deferred_until(campaign, MessageChannel.sms, now=at(12, 10)) is None
    assert MessagingService.review_sms_deferred_until(campaign, MessageChannel.telegram, now=at(11, 23)) is None
    assert MessagingService.next_day_review_send_at(at(11)) == at(12, 10)
