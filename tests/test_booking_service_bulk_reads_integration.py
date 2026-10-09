"""BS-07 service-read contracts on isolated PostgreSQL, without app startup.

The shared database fixture requires explicit local SEGMENTS_TEST_DATABASE_URL
and owns a fresh schema per case. Seed writes finish before measured operations;
comparisons use a real repeatable-read, read-only transaction. No provider calls,
result fakes, broken foreign keys, or production runtime overrides are involved.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text, update

import app.services.booking as booking_module
import app.services.booking_alternatives as alternatives_module
from app.models.booking import (
    BarberService, BaseService, Booking, BookingStatus, Master,
    MasterAvailabilityWindow, MasterTimeBlock,
)
from app.schemas.booking import BarberServiceResponse
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.services.booking import BookingServiceLayer, KYIV_TZ
from app.services.booking_alternatives import BookingAlternativesService
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


AT = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)
FIRST = datetime(2026, 10, 10, 10, tzinfo=KYIV_TZ)
MISSING = 999999


class OriginalLoopBookingService(BookingServiceLayer):
    """Pre-BS-07 orchestration, retaining the real single-service SQL reader."""
    async def get_active_services(self, session, service_ids):
        services_by_id = {}
        for service_id in service_ids:
            item = await self.get_active_service(session, service_id)
            services_by_id[item.id] = item
        return [services_by_id[service_id] for service_id in service_ids]


def base_fact(index):
    return dict(id=index, name=f"Base {index:02d}", title_uk=None, title_en=None,
                description=None, description_uk=None, description_en=None,
                duration_minutes=10 + 5 * (index % 3), price=100 + index,
                is_active=index != 21)


def service_fact(master, index):
    return dict(id=master * 1000 + index, master_id=master, base_service_id=index,
                name=f"Service {master}/{index:02d}", duration_minutes=(10 if master == 3 else 10 + 5 * (index % 3)),
                price=100 * master + index, is_active=True,
                created_at=AT, updated_at=AT)


def expected_dto(fact):
    """Expected public values come from seed facts, never the queried ORM row."""
    base = base_fact(fact['base_service_id']) if fact['base_service_id'] is not None else None
    return BarberServiceResponse.model_validate({
        **fact, 'barber_id': fact['master_id'],
        'source_type': 'base' if base else 'custom', 'description': None,
        'base_service': base,
    }).model_dump(mode='json')


def dtos(items):
    return [BarberServiceResponse.model_validate(item).model_dump(mode='json') for item in items]


@pytest.fixture
async def catalog(database, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return AT.astimezone(tz) if tz else AT.replace(tzinfo=None)

    monkeypatch.setattr(booking_module, 'datetime', FrozenDateTime)
    monkeypatch.setattr(alternatives_module, 'datetime', FrozenDateTime)
    facts = {master * 1000 + index: service_fact(master, index)
             for master in (1, 2, 3, 4) for index in range(1, 21)}
    facts.update({
        9001: dict(id=9001, master_id=1, base_service_id=None, name='Inactive custom',
                   duration_minutes=15, price=10, is_active=False, created_at=AT, updated_at=AT),
        9002: dict(id=9002, master_id=1, base_service_id=21, name='Inactive base link',
                   duration_minutes=15, price=20, is_active=True, created_at=AT, updated_at=AT),
        9003: dict(id=9003, master_id=1, base_service_id=None, name='Active custom one',
                   duration_minutes=25, price=305, is_active=True, created_at=AT, updated_at=AT),
        9004: dict(id=9004, master_id=1, base_service_id=None, name='Active custom two',
                   duration_minutes=40, price=405, is_active=True, created_at=AT, updated_at=AT),
    })
    async with database() as session:
        session.add_all([
            Master(id=1, full_name='Primary synthetic', is_active=True, show_on_master_block=True),
            Master(id=2, full_name='Other synthetic', is_active=True, show_on_master_block=True),
            Master(id=3, full_name='Hidden calendar', is_active=True, show_on_master_block=False),
            Master(id=4, full_name='Redirect alias', is_active=False, show_on_master_block=True),
        ])
        session.add_all([BaseService(**base_fact(index)) for index in range(1, 22)])
        await session.flush()
        await session.execute(update(Master).where(Master.id == 4).values(booking_redirect_master_id=3))
        session.add_all([BarberService(**fact) for fact in facts.values()])
        await session.flush()
        for master in (1, 2, 3):
            for offset in (0, 1):
                start = FIRST + timedelta(days=offset)
                session.add(MasterAvailabilityWindow(master_id=master, start_at=start,
                                                      end_at=start + timedelta(minutes=75)))
        session.add(Booking(master_id=1, service_id=1001, customer_name='Synthetic',
                            customer_phone='+380000000001', start_at=FIRST + timedelta(minutes=15),
                            end_at=FIRST + timedelta(minutes=30), status=BookingStatus.confirmed))
        session.add(MasterTimeBlock(master_id=2, start_at=FIRST + timedelta(minutes=45),
                                   end_at=FIRST + timedelta(minutes=75), reason='Synthetic'))
        await session.commit()
    return facts


async def begin_snapshot(session):
    await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
    assert await session.scalar(text('SHOW transaction_read_only')) == 'on'


@contextmanager
def observed_reads(database):
    statements = []
    engine = database.kw['bind'].sync_engine

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', record)
    try:
        yield statements
    finally:
        event.remove(engine, 'before_cursor_execute', record)


def assert_only_selects(statements):
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in statements), statements


@pytest.mark.anyio
@pytest.mark.parametrize('count', [0, 1, 5, 20], ids=['empty', 'single', 'five', 'twenty'])
async def test_selected_service_reads_preserve_complete_dto_with_bounded_queries(database, catalog, count, record_property):
    ids = list(range(1001, 1001 + count))
    expected = [expected_dto(catalog[identity]) for identity in ids]
    async with database() as session:
        await begin_snapshot(session)
        with observed_reads(database) as original_sql:
            original = await OriginalLoopBookingService().get_active_services(session, ids)
            original_dtos = dtos(original)
        session.expunge_all()  # Fresh identity map, same database snapshot.
        with observed_reads(database) as current_sql:
            current = await BookingServiceLayer().get_active_services(session, ids)
            current_dtos = dtos(current)
        assert current_dtos == original_dtos == expected
        assert BookingServiceLayer().resolve_duration_minutes(current, None) == sum(catalog[i]['duration_minutes'] for i in ids)
        assert len(original_sql) == 2 * count
        assert len(current_sql) == (2 if count else 0)
        assert_only_selects(original_sql)
        assert_only_selects(current_sql)
        assert not session.new and not session.dirty and not session.deleted
        record_property('service_select_counts', {'original': len(original_sql), 'current': len(current_sql)})


@pytest.mark.anyio
@pytest.mark.parametrize('ids,expected_queries', [
    ([1005, 1001, 1003], 2),
    ([1001, 1001, 1005], 2),
    ([9003, 1002, 1001, 9004], 2),
    ([9004, 9003], 1),
], ids=['reversed', 'duplicates', 'mixed-base-custom', 'custom-only'])
async def test_bulk_reads_preserve_caller_order_duplicates_and_custom_services(database, catalog, ids, expected_queries):
    async with database() as session:
        await begin_snapshot(session)
        original = dtos(await OriginalLoopBookingService().get_active_services(session, ids))
        session.expunge_all()
        with observed_reads(database) as statements:
            items = await BookingServiceLayer().get_active_services(session, ids)
            result = dtos(items)
        assert result == original == [expected_dto(catalog[identity]) for identity in ids]
        assert [item.id for item in items] == ids
        assert BookingServiceLayer().resolve_duration_minutes(items, None) == sum(catalog[i]['duration_minutes'] for i in ids)
        assert len(statements) == expected_queries
        assert_only_selects(statements)
        if ids[0] == ids[1]:
            assert items[0] is items[1]


@pytest.mark.anyio
@pytest.mark.parametrize('ids', [
    [MISSING, 1001], [1001, MISSING],
    [9001, 1001], [1001, 9001],
    [9002, 1001], [1001, 9002], [9002],
], ids=['missing-first', 'missing-last', 'inactive-first', 'inactive-last',
        'inactive-base-first', 'inactive-base-last', 'single-inactive-base'])
async def test_unavailable_service_preserves_404_without_partial_result(database, catalog, ids):
    async with database() as session:
        await begin_snapshot(session)
        outcomes = []
        for service in (OriginalLoopBookingService(), BookingServiceLayer()):
            session.expunge_all()
            with observed_reads(database) as statements:
                with pytest.raises(HTTPException) as caught:
                    await service.get_active_services(session, ids)
            # Assertions are outside the caught call; a broken assertion cannot
            # be mistaken for an expected lookup failure.
            outcomes.append((caught.value.status_code, caught.value.detail))
            assert_only_selects(statements)
            assert not session.new and not session.dirty and not session.deleted
        assert outcomes == [(404, 'Service not found'), (404, 'Service not found')]


@pytest.mark.anyio
async def test_bulk_read_duration_mismatch_keeps_canonical_400(database, catalog):
    async with database() as session:
        await begin_snapshot(session)
        service = BookingServiceLayer()
        items = await service.get_active_services(session, [1002, 1001])
        assert service.resolve_duration_minutes(items, None) == 35
        assert service.resolve_duration_minutes(items, 35) == 35
        with pytest.raises(HTTPException) as caught:
            service.resolve_duration_minutes(items, 36)
        assert caught.value.status_code == 400
        assert caught.value.detail == 'duration_minutes must equal the selected services duration'


@pytest.mark.anyio
async def test_reading_active_services_does_not_bypass_master_ownership(database, catalog):
    async with database() as session:
        await begin_snapshot(session)
        service = BookingServiceLayer()
        source = await service.get_active_master_with_services(session, 1)
        items = await service.get_active_services(session, [2002, 2001])
        assert [item.master_id for item in items] == [2, 2]
        with pytest.raises(HTTPException) as caught:
            service.ensure_master_provides_services(source, [item.id for item in items])
        assert caught.value.status_code == 400
        assert caught.value.detail == 'Master does not provide this service'


@pytest.mark.anyio
async def test_redirect_uses_ordered_source_ids_duration_and_mapped_price(database, catalog):
    async with database() as seed:
        await seed.execute(update(Master).where(Master.id == 4).values(is_active=True))
        await seed.commit()
    payload = BookingAlternativesRequest(master_id=4, service_ids=[4002, 4001], duration_minutes=35,
        desired_date=AT.astimezone(KYIV_TZ).date(), another_master_acceptable=False, page_size=20)
    async with database() as session:
        await begin_snapshot(session)
        service = BookingServiceLayer()
        requested, calendar = await service.resolve_booking_master(session, 4)
        selected = await service.get_active_services(session, payload.service_ids)
        mapped = await service.resolve_booking_services_for_master(session, requested, calendar,
            payload.service_ids, source_services=selected)
        assert (requested.id, calendar.id) == (4, 3)
        assert [item.id for item in selected] == [4002, 4001]
        assert [item.id for item in mapped] == [3002, 3001]
        assert service.resolve_duration_minutes(selected, None) == 35
        assert sum(item.duration_minutes for item in mapped) == 20
        assert sum(item.price for item in mapped) == 603
        original = await BookingAlternativesService(OriginalLoopBookingService()).find_page(session, payload)
        session.expunge_all()
        actual = await BookingAlternativesService(service).find_page(session, payload)
        assert actual.model_dump(mode='json') == original.model_dump(mode='json')
        assert actual.total_count == len(actual.same_master) == 6
        assert actual.other_masters == []
        assert all(item.master.id == 4 and item.service_ids == [4002, 4001]
                   and item.duration_minutes == 35 and item.price == Decimal(603)
                   for item in actual.same_master)


@pytest.mark.anyio
async def test_real_paged_alternatives_equal_original_full_result_without_missing_offers(database, catalog):
    payload = BookingAlternativesRequest(master_id=1, service_ids=[1002, 1001], duration_minutes=35,
        desired_date=AT.astimezone(KYIV_TZ).date(), page_size=3)
    async with database() as session:
        await begin_snapshot(session)
        original_service = BookingAlternativesService(OriginalLoopBookingService())
        full = await original_service.find(session, payload)
        full_slots = [*full.same_master, *full.other_masters]
        assert len(full_slots) == 8  # Two 75-minute days, minus one real booking and one block.
        expected_starts = {
            (1, FIRST + timedelta(minutes=30)), (2, FIRST),
            *((master, FIRST + timedelta(days=1, minutes=minute))
              for master in (1, 2) for minute in (0, 15, 30)),
        }
        assert {(item.master.id, item.start_at) for item in full_slots} == expected_starts
        session.expunge_all()
        actual_service = BookingAlternativesService(BookingServiceLayer())
        seen = []
        offset = 0
        while True:
            request = payload.model_copy(update={'offset': offset})
            actual = await actual_service.find_page(session, request)
            expected = original_service.paginate(full, offset=offset, page_size=payload.page_size)
            assert actual.model_dump(mode='json') == expected.model_dump(mode='json')
            assert actual.total_count == 8
            for item in [*actual.same_master, *actual.other_masters]:
                assert item.duration_minutes == 35
                assert item.service_ids == [item.master.id * 1000 + 2, item.master.id * 1000 + 1]
                assert item.price == Decimal(200 * item.master.id + 3)
                seen.append(item.offer_id)
            if actual.next_offset is None:
                break
            assert actual.next_offset > offset
            offset = actual.next_offset
            assert offset < 8
        assert len(seen) == len(set(seen)) == 8
        assert set(seen) == {item.offer_id for item in full_slots}
        beyond = await actual_service.find_page(session, payload.model_copy(update={'offset': 9}))
        assert beyond.same_master == beyond.other_masters == []
        assert beyond.next_offset is None and beyond.total_count == 8
        assert not session.new and not session.dirty and not session.deleted
