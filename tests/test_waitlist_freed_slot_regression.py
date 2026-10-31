"""Freed-slot snapshots must never advertise occupied time or duplicate a hold."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.v1.routes.bookings import schedule_rescheduled_waitlist_offers
from app.services.waitlist_offers import FreedBookingSlot, WaitlistOfferService


@pytest.fixture
def anyio_backend():
    return 'asyncio'


START = datetime(2099, 1, 2, 10, tzinfo=UTC)


@pytest.mark.parametrize(('new_start', 'new_end', 'expected'), [
    (0, 60, []),  # unchanged schedule, including service-only edits
    (-15, 75, []),  # extending a booking frees nothing
    (30, 90, [(0, 30)]),
    (-30, 30, [(30, 60)]),
    (15, 45, [(0, 15), (45, 60)]),
    (60, 120, [(0, 60)]),
    (-60, 0, [(0, 60)]),
])
def test_reschedule_only_schedules_released_intervals(new_start, new_end, expected):
    tasks = BackgroundTasks()
    slot = FreedBookingSlot(7, START, START + timedelta(minutes=60), 42, 3)
    schedule_rescheduled_waitlist_offers(
        tasks, slot, START + timedelta(minutes=new_start), START + timedelta(minutes=new_end),
    )
    slots = [task.args[0] for task in tasks.tasks]
    assert [(int((s.start_at - START).total_seconds() / 60),
             int((s.end_at - START).total_seconds() / 60)) for s in slots] == expected
    assert all((s.master_id, s.source_booking_id, s.source_master_id) == (7, 42, 3) for s in slots)


def test_nonconfirmed_edit_has_no_freed_slot():
    tasks = BackgroundTasks()
    schedule_rescheduled_waitlist_offers(tasks, None, START, START + timedelta(hours=1))
    assert tasks.tasks == []


def flow(*, held=None, busy=(), blocks=()):
    service = WaitlistOfferService()
    service._now = lambda: START - timedelta(days=1)
    service.booking_service.get_active_master_with_services = AsyncMock(return_value=SimpleNamespace(id=7))
    service.booking_service.ensure_booking_within_availability = AsyncMock()
    # Use the real overlap validator: a moved booking is NOT excluded.
    service.booking_service.list_busy_bookings = AsyncMock(return_value=busy)
    service.booking_service.list_time_blocks = AsyncMock(return_value=blocks)
    service._eligible_requests = AsyncMock(return_value=[])
    service._send_offer = AsyncMock()
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: held)),
        commit=AsyncMock(), rollback=AsyncMock(), add=AsyncMock(), flush=AsyncMock(),
    )
    return service, session


@pytest.mark.anyio
@pytest.mark.parametrize(('busy', 'blocks'), [([SimpleNamespace(id=42)], []), ([], [SimpleNamespace(id=8)])])
async def test_stale_snapshot_with_booking_or_block_rolls_back_before_candidates(busy, blocks):
    service, session = flow(busy=busy, blocks=blocks)
    assert await service.offer_slot(session, master_id=7, start_at=START,
                                    end_at=START + timedelta(hours=1)) is None
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    service._eligible_requests.assert_not_awaited()
    service._send_offer.assert_not_awaited()
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    service.booking_service.list_busy_bookings.assert_awaited_once_with(
        session, 7, START, START + timedelta(hours=1), None,
    )


@pytest.mark.anyio
async def test_repeated_processing_of_existing_hold_never_sends_or_selects_again():
    service, session = flow(held=99, busy=[SimpleNamespace(id=99)])
    for _ in range(2):
        assert await service.offer_slot(session, master_id=7, start_at=START,
                                        end_at=START + timedelta(hours=1)) is None
    assert session.commit.await_count == 2
    service.booking_service.list_busy_bookings.assert_not_awaited()
    service._eligible_requests.assert_not_awaited()
    service._send_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_free_candidate_still_runs_overlap_validation():
    service, session = flow()
    assert await service.offer_slot(session, master_id=7, start_at=START,
                                    end_at=START + timedelta(hours=1)) is None
    service.booking_service.list_busy_bookings.assert_awaited_once()
    service.booking_service.list_time_blocks.assert_awaited_once()
    service._eligible_requests.assert_awaited_once()
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('error', [HTTPException(400, 'invalid availability'), RuntimeError('database failure')])
async def test_unexpected_validation_failure_still_propagates(error):
    service, session = flow()
    service.booking_service.ensure_slot_available = AsyncMock(side_effect=error)
    with pytest.raises(type(error)) as raised:
        await service.offer_slot(session, master_id=7, start_at=START, end_at=START + timedelta(hours=1))
    assert raised.value is error
    service._eligible_requests.assert_not_awaited()
    service._send_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_closed_availability_is_a_stale_snapshot():
    service, session = flow()
    service.booking_service.ensure_booking_within_availability = AsyncMock(
        side_effect=HTTPException(409, "Booking slot is outside master's open availability"),
    )
    assert await service.offer_slot(session, master_id=7, start_at=START,
                                    end_at=START + timedelta(hours=1)) is None
    session.rollback.assert_awaited_once()
    service.booking_service.list_busy_bookings.assert_not_awaited()
    service._eligible_requests.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('admin', [False, True])
@pytest.mark.parametrize(('offset', 'commit_fails'), [(0, False), (30, False), (30, True)])
async def test_edit_routes_schedule_only_after_successful_commit(monkeypatch, admin, offset, commit_fails):
    from app.api.v1.routes import bookings as routes
    from app.models.booking import BookingStatus
    from app.schemas.booking import AdminBookingUpdate, BookingUpdate

    booking = SimpleNamespace(id=42, master_id=7, redirected_from_master_id=None,
                              status=BookingStatus.confirmed, start_at=START,
                              end_at=START + timedelta(hours=1))
    tasks = BackgroundTasks()

    async def commit():
        assert tasks.tasks == []
        if commit_fails:
            raise RuntimeError('synthetic commit failure')

    result = SimpleNamespace(scalar_one_or_none=lambda: booking, scalar_one=lambda: booking)
    session = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock(side_effect=commit))
    for name in ('ensure_not_past', 'ensure_within_working_hours', 'ensure_within_open_business_days'):
        monkeypatch.setattr(routes.service, name, lambda *_: None)
    for name in ('ensure_booking_within_availability', 'ensure_slot_available'):
        monkeypatch.setattr(routes.service, name, AsyncMock())
    monkeypatch.setattr(routes.service.promotion_service, 'revalidate_first_visit_booking', AsyncMock())
    monkeypatch.setattr(routes, 'BookingBackofficeResponse', SimpleNamespace(model_validate=lambda b: b))
    payload_type = AdminBookingUpdate if admin else BookingUpdate
    kwargs = dict(booking_id=42, session=session, background_tasks=tasks,
                  payload=payload_type(start_at=START + timedelta(minutes=offset),
                                       end_at=START + timedelta(minutes=60 + offset)))
    if admin:
        route = routes.admin_update_booking
        kwargs['current_user'] = SimpleNamespace(is_superuser=True)
    else:
        route = routes.update_my_booking
        kwargs['current_master'] = SimpleNamespace(id=7, booking_redirect_master_id=None)
    if commit_fails:
        with pytest.raises(RuntimeError, match='synthetic commit failure'):
            await route(**kwargs)
        assert tasks.tasks == []
    else:
        await route(**kwargs)
        assert len(tasks.tasks) == (1 if offset else 0)
        if offset:
            assert tasks.tasks[0].args[0] == FreedBookingSlot(7, START, START + timedelta(minutes=30), 42, 7)
    routes.service.ensure_slot_available.assert_awaited_once_with(
        session, 7, START + timedelta(minutes=offset), START + timedelta(minutes=60 + offset),
        exclude_booking_id=42,
    )
