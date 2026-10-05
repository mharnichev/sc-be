from datetime import date, datetime, timedelta
import asyncio
from decimal import Decimal
import os
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import all_models  # noqa: F401
from app.models.booking import BarberService, Master, MasterAvailabilityWindow
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.services.booking import BookingServiceLayer
from app.services.booking_alternatives import BookingAlternativesService
from app.schemas.booking import PublicBookingCreate
from tests.test_booking_alternatives_optimization import MemoryBooking, MemorySession, memory_master

KYIV = ZoneInfo("Europe/Kyiv")


def test_request_rejects_duplicate_or_invalid_service_ids() -> None:
    with pytest.raises(ValidationError):
        BookingAlternativesRequest(master_id=1, service_ids=[1, 1], desired_date=date.today(), duration_minutes=30)
    with pytest.raises(ValidationError):
        BookingAlternativesRequest(master_id=0, service_ids=[1], desired_date=date.today(), duration_minutes=30)


def test_public_slot_keeps_kyiv_date_boundary() -> None:
    service = BookingAlternativesService()
    master = SimpleNamespace(id=1, full_name_uk="Майстер", photo_url=None, avatar_url=None, position_uk="Майстер")
    start = datetime(2026, 8, 6, 0, 15, tzinfo=KYIV)
    slot = service._public_master(master)
    assert slot.name == "Майстер"
    assert start.astimezone(KYIV).date() == date(2026, 8, 6)


def test_matching_services_requires_every_selected_service() -> None:
    service = BookingAlternativesService()
    selected = [
        SimpleNamespace(id=10, base_service_id=1, is_active=True),
        SimpleNamespace(id=11, base_service_id=2, is_active=True),
    ]
    candidate = SimpleNamespace(services=[SimpleNamespace(id=20, base_service_id=1, is_active=True)])
    assert service._matching_service_ids(candidate, selected) is None


@pytest.mark.anyio
async def test_no_alternatives_is_an_empty_response() -> None:
    booking = MemoryBooking([memory_master(1)])
    result = await BookingAlternativesService(booking).find(
        MemorySession(booking), booking.request(another_master_acceptable=False),
    )
    assert result.same_master == []
    assert result.other_masters == []
    assert booking.bulk_calls == [{1}]


@pytest.mark.anyio
async def test_same_master_alternative_starts_after_desired_date_and_uses_total_duration() -> None:
    master = memory_master(1, duration=30, price=Decimal("100.50"), base_id=100)
    master.services.append(SimpleNamespace(
        id=11, master_id=1, name="Beard", title_uk="Beard", title_en=None,
        duration_minutes=45, price=Decimal("200.25"), is_active=True,
        base_service_id=101, base_service=SimpleNamespace(is_active=True),
    ))
    booking = MemoryBooking([master])
    booking.open(1, booking.target + timedelta(days=1))
    result = await BookingAlternativesService(booking).find(
        MemorySession(booking),
        BookingAlternativesRequest(
            master_id=1,
            service_ids=[10, 11],
            desired_date=booking.target,
            duration_minutes=75,
            another_master_acceptable=False,
        ),
    )

    assert booking.bulk_calls == [{1}]
    assert result.same_master[0].date == booking.target + timedelta(days=1)
    assert result.same_master[0].duration_minutes == 75
    assert result.same_master[0].end_at - result.same_master[0].start_at == timedelta(minutes=75)
    assert result.same_master[0].service_ids == [10, 11]
    assert result.same_master[0].price == Decimal("300.75")
    assert result.same_master[0].recommended is True


def test_recommendations_prioritize_daypart_diversity_before_filling_limit() -> None:
    service = BookingAlternativesService()
    master = service._public_master(SimpleNamespace(id=1, full_name_uk="Майстер"))
    target = datetime(2099, 1, 2, 10, tzinfo=KYIV)
    slots = [
        service_slot(master, target),
        service_slot(master, target + timedelta(minutes=15)),
        service_slot(master, target.replace(hour=14)),
        service_slot(master, target.replace(hour=18)),
    ]

    recommended = service._recommend(slots, limit=3, diversify_masters=False)

    assert {service._daypart(item) for item in recommended if item.recommended} == {
        "morning", "afternoon", "evening",
    }


def test_pagination_preserves_every_offer_without_duplicate_or_cutoff() -> None:
    from app.schemas.booking_alternatives import BookingAlternativesResponse

    service = BookingAlternativesService()
    master_one = service._public_master(SimpleNamespace(id=1, full_name_uk="Перший"))
    master_two = service._public_master(SimpleNamespace(id=2, full_name_uk="Другий"))
    start = datetime(2099, 1, 2, 10, tzinfo=KYIV)
    same_recommended = service_slot(master_one, start).model_copy(update={"recommended": True})
    same_remaining = service_slot(master_one, start + timedelta(hours=3))
    other_recommended = service_slot(master_two, start + timedelta(hours=1)).model_copy(update={"recommended": True})
    other_remaining = service_slot(master_two, start + timedelta(hours=2))
    response = BookingAlternativesResponse(
        search_context_id="a" * 32,
        same_master=[same_recommended, same_remaining],
        other_masters=[other_recommended, other_remaining],
    )

    first = service.paginate(response, offset=0, page_size=3)
    second = service.paginate(response, offset=3, page_size=3)
    beyond = service.paginate(response, offset=99, page_size=3)
    offer_ids = [
        *(item.offer_id for item in first.same_master),
        *(item.offer_id for item in first.other_masters),
        *(item.offer_id for item in second.same_master),
        *(item.offer_id for item in second.other_masters),
    ]

    assert first.total_count == second.total_count == 4
    assert first.next_offset == 3
    assert second.next_offset is None
    assert set(offer_ids) == {item.offer_id for item in [same_recommended, same_remaining, other_recommended, other_remaining]}
    assert len(offer_ids) == len(set(offer_ids)) == 4
    assert beyond.same_master == beyond.other_masters == []
    assert beyond.total_count == 4
    assert beyond.next_offset is None


def service_slot(master, start_at: datetime):
    from app.schemas.booking_alternatives import BookingAlternativeSlot

    return BookingAlternativeSlot(
        master=master,
        service_ids=[1],
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        date=start_at.date(),
        duration_minutes=30,
        price=Decimal("100"),
        search_context_id="a" * 32,
        offer_id=f"{start_at.hour:02d}{start_at.minute:02d}".ljust(32, "0"),
    )


@pytest.mark.anyio
async def test_redirected_alternatives_keep_public_source_identity_and_canonical_duration() -> None:
    requested = memory_master(1, 60, 500, redirect=8)
    different_duration = memory_master(2, 45, 600, redirect=8)
    same_duration = memory_master(3, 60, 700, redirect=8)
    technical_hop = memory_master(8, 45, 800, redirect=9, public=False)
    calendar = memory_master(9, 45, 800, public=False)
    booking = MemoryBooking([requested, different_duration, same_duration, technical_hop, calendar])
    booking.open(9, booking.target)
    booking.open(9, booking.target + timedelta(days=1))

    result = await BookingAlternativesService(booking).find(
        MemorySession(booking), booking.request(1, another_master_acceptable=True),
    )

    assert booking.bulk_calls == [{9}]
    assert result.same_master[0].master.id == requested.id
    assert result.same_master[0].master.name == requested.full_name_uk
    assert result.same_master[0].duration_minutes == 60
    assert result.same_master[0].price == Decimal("800")
    assert {item.master.id for item in result.other_masters} == {different_duration.id}
    assert result.other_masters[0].master.name == different_duration.full_name_uk
    assert result.other_masters[0].duration_minutes == 45
    assert result.other_masters[0].service_ids == [different_duration.services[0].id]
    # A candidate is evaluated using its own catalog duration instead of being
    # rejected just because it differs from the originally selected master.
    assert same_duration.services[0].duration_minutes == 60
    # The public candidates share the technical calendar/start and therefore
    # produce one physical offer rather than a duplicate.
    assert len({item.start_at for item in result.other_masters}) == len(result.other_masters)
    assert result.other_masters[0].offer_id == BookingAlternativesService(booking)._opaque_id(
        "booking-alternative-offer-v1",
        result.search_context_id,
        9,
        result.other_masters[0].start_at.astimezone(KYIV).isoformat(),
        45,
    )


@pytest.mark.anyio
async def test_unbookable_redirect_candidate_does_not_hide_valid_alternatives() -> None:
    requested = memory_master(1, 60, 500, redirect=8)
    valid_candidate = memory_master(2, 60, 600, redirect=8)
    invalid_candidate = memory_master(3, 60, 600, redirect=99)
    technical_hop = memory_master(8, 45, 800, redirect=9, public=False)
    calendar = memory_master(9, 45, 800, public=False)
    booking = MemoryBooking([requested, valid_candidate, invalid_candidate, technical_hop, calendar])
    booking.open(9, booking.target)
    response = await BookingAlternativesService(booking).find(
        MemorySession(booking), booking.request(1, another_master_acceptable=True),
    )

    assert {item.master.id for item in response.other_masters} == {valid_candidate.id}
    assert booking.bulk_calls == [{9}]


@pytest.mark.anyio
async def test_private_selected_master_is_not_exposed_as_an_alternative() -> None:
    master = memory_master(1, 30, 500)
    master.show_on_master_block = False
    booking = MemoryBooking([master])

    with pytest.raises(HTTPException) as exc_info:
        await BookingAlternativesService(booking).find(
            MemorySession(booking), booking.request(1, another_master_acceptable=False),
        )

    assert exc_info.value.status_code == 404


class RangeSlotService(BookingServiceLayer):
    def __init__(self, target: date) -> None:
        super().__init__()
        self.target = target
        self.master = SimpleNamespace(id=1, services=[])
        self.service = SimpleNamespace(id=10, duration_minutes=30, price=100, is_active=True)
        self.master.services = [self.service]
        self.calendar_reads = 0

    def availability_horizon_end_date(self) -> date:
        return self.target + timedelta(days=10)

    async def resolve_booking_master(self, _session, _master_id):
        return self.master, self.master

    async def get_active_services(self, _session, _service_ids):
        return [self.service]

    async def resolve_booking_services_for_master(self, *_args, **_kwargs):
        return [self.service]

    async def list_availability_windows(self, _session, _master_id, start_at, end_at):
        self.calendar_reads += 1
        return [SimpleNamespace(start_at=start_at, end_at=end_at)]

    async def list_busy_bookings(self, _session, _master_id, start_at, _end_at):
        self.calendar_reads += 1
        blocked_start, blocked_end = self.day_bounds(start_at.date() + timedelta(days=1))
        return [SimpleNamespace(start_at=blocked_start, end_at=blocked_end)]

    async def list_time_blocks(self, *_args):
        self.calendar_reads += 1
        return []


@pytest.mark.anyio
async def test_range_availability_delegates_one_normalized_context() -> None:
    """The range query preloads calendar records once instead of once per day."""
    target = datetime.now(KYIV).date() + timedelta(days=1)
    while target.weekday() == 0:
        target += timedelta(days=1)
    booking = RangeSlotService(target)
    result = await booking.get_available_slots_range(
        None,
        master_id=1,
        start_date=target,
        end_date=target + timedelta(days=1),
        service_ids=[10],
        duration_minutes=30,
    )

    assert booking.calendar_reads == 3
    assert result == [
        (target, True),
        (target + timedelta(days=1), False),
    ]


@pytest.mark.anyio
async def test_hidden_requested_master_cannot_use_range_availability() -> None:
    target = datetime.now(KYIV).date() + timedelta(days=1)
    while target.weekday() == 0:
        target += timedelta(days=1)
    booking = RangeSlotService(target)
    booking.master.show_on_master_block = False

    with pytest.raises(HTTPException) as exc_info:
        await booking.get_available_slots_for_range(
            None, 1, target, target, service_ids=[10], duration_minutes=30
        )

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_single_day_and_range_apply_the_same_horizon_policy() -> None:
    target = datetime.now(KYIV).date() + timedelta(days=1)
    while target.weekday() == 0:
        target += timedelta(days=1)
    booking = RangeSlotService(target)
    outside_horizon = booking.availability_horizon_end_date() + timedelta(days=1)

    with pytest.raises(HTTPException) as single_error:
        await booking.get_available_slots(None, 1, 10, outside_horizon)
    with pytest.raises(HTTPException) as range_error:
        await booking.get_available_slots_range(None, 1, outside_horizon, outside_horizon, service_ids=[10])

    assert single_error.value.status_code == range_error.value.status_code == 400
    assert single_error.value.detail == range_error.value.detail == "date range is outside availability horizon"


@pytest.fixture
async def availability_database():
    """Isolated PostgreSQL schema; never falls back to the application database."""
    url = os.environ.get("BOOKING_AVAILABILITY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set BOOKING_AVAILABILITY_TEST_DATABASE_URL to an isolated local PostgreSQL database")
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1", "::1", "postgres"} or "test" not in (parsed.database or ""):
        pytest.fail("Integration database must be an explicit isolated local/test-container database")
    schema = "availability_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.anyio
async def test_range_calendar_query_count_is_constant_per_range(availability_database) -> None:
    """The number of reads stays bounded as days are added to one range request."""
    engine, sessions = availability_database
    service = BookingServiceLayer()
    start_date = datetime.now(KYIV).date() + timedelta(days=1)
    while start_date.weekday() == 0:
        start_date += timedelta(days=1)
    end_date = start_date + timedelta(days=2)
    async with sessions() as session:
        master = Master(full_name="Range query master", is_active=True)
        session.add(master)
        await session.flush()
        barber_service = BarberService(
            master_id=master.id,
            name="Range query service",
            duration_minutes=30,
            price=100,
            is_active=True,
        )
        first_start, first_end = service.day_bounds(start_date)
        last_start, last_end = service.day_bounds(end_date)
        session.add_all([
            barber_service,
            MasterAvailabilityWindow(master_id=master.id, start_at=first_start, end_at=first_end),
            MasterAvailabilityWindow(master_id=master.id, start_at=last_start, end_at=last_end),
        ])
        await session.commit()

        statements: list[str] = []

        def count_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
        try:
            result = await service.get_available_slots_range(
                session,
                master.id,
                start_date,
                end_date,
                service_ids=[barber_service.id],
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_statement)

    assert result == [(start_date, True), (start_date + timedelta(days=1), False), (end_date, True)]
    # Master/services plus one each for windows, bookings, holds and blocks;
    # this must not grow with the three requested dates.
    assert len(statements) <= 8


@pytest.mark.anyio
async def test_independent_transactions_create_only_one_booking_for_one_slot(availability_database) -> None:
    """The master row lock serializes competing writes from independent sessions."""
    _engine, sessions = availability_database
    service = BookingServiceLayer()
    target_date = datetime.now(KYIV).date() + timedelta(days=1)
    while target_date.weekday() == 0:
        target_date += timedelta(days=1)
    start_at, end_at = service.day_bounds(target_date)
    async with sessions() as setup:
        master = Master(full_name="Concurrent availability master", is_active=True)
        setup.add(master)
        await setup.flush()
        barber_service = BarberService(
            master_id=master.id,
            name="Concurrent availability service",
            duration_minutes=30,
            price=100,
            is_active=True,
        )
        setup.add_all([
            barber_service,
            MasterAvailabilityWindow(master_id=master.id, start_at=start_at, end_at=end_at),
        ])
        await setup.commit()

    async def attempt(index: int):
        async with sessions() as session:
            try:
                booking = await BookingServiceLayer().create_public_booking(
                    session,
                    PublicBookingCreate(
                        master_id=master.id,
                        service_ids=[barber_service.id],
                        start_at=start_at,
                        customer_name=f"Concurrent Customer {index}",
                        customer_phone=f"+38050123{index:04d}",
                    ),
                )
                return ("created", booking.id)
            except HTTPException as exc:
                return (exc.status_code, exc.detail)

    first, second = await asyncio.gather(attempt(1), attempt(2))
    outcomes = [first, second]
    assert sum(item[0] == "created" for item in outcomes) == 1
    assert sum(item[0] == 409 for item in outcomes) == 1
