"""Telegram-only privacy, ordered cohorts and transaction isolation on PostgreSQL."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
import importlib.util
from pathlib import Path
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_inventory_integration import database, anyio_backend  # noqa: F401
from app.models.booking import Booking, BookingSource, BookingStatus, BarberService, Master
from app.models.telegram_booking_funnel import TelegramBookingFunnelEvent as Event
from app.schemas.telegram_booking_funnel import TELEGRAM_FUNNEL_STEPS
from app.services.telegram_booking_funnel import _attempt_hash, aggregate_telegram_funnel, record_telegram_step

pytestmark = pytest.mark.anyio
AT = datetime(2026, 10, 11, 21, 5, tzinfo=UTC)  # Oct 12 Kyiv.


def bot(attempt=None):
    return SimpleNamespace(payload_json={"telegram_funnel_attempt_id": attempt or str(uuid4())})


async def booking(session, source=BookingSource.telegram, status=BookingStatus.confirmed):
    master = Master(full_name="Synthetic")
    session.add(master)
    await session.flush()
    service = BarberService(master_id=master.id, name="Synthetic", price=100, duration_minutes=30)
    session.add(service)
    await session.flush()
    item = Booking(master_id=master.id, service_id=service.id, customer_name="Synthetic", customer_phone="synthetic",
        start_at=AT, end_at=AT+timedelta(minutes=30), source=source, status=status,
        completed_at=AT if status==BookingStatus.completed else None)
    session.add(item)
    await session.flush()
    return item


async def emit(session, state, steps, item=None, at=AT):
    for index, step in enumerate(steps):
        assert await record_telegram_step(session, state, step, booking_id=item.id if step=="booking_success" else None)
        event = (await session.execute(select(Event).where(Event.attempt_hash==_attempt_hash(state.payload_json["telegram_funnel_attempt_id"]), Event.step==step))).scalar_one()
        event.occurred_at=at+timedelta(seconds=index)
    await session.commit()


async def aggregate(session, day=date(2026,10,12)):
    return await aggregate_telegram_funnel(session, date_from=day, date_to=day)


async def test_complete_deduplicated_private_funnel_tracks_actual_completed_booking(database):
    async with database() as session:
        state=bot()
        item=await booking(session,status=BookingStatus.completed)
        await emit(session,state,TELEGRAM_FUNNEL_STEPS,item)
        for step in TELEGRAM_FUNNEL_STEPS:
            assert not await record_telegram_step(session,state,step,booking_id=item.id if step=="booking_success" else None)
        await session.commit()
        result=await aggregate(session)
        assert result.started==result.succeeded==result.completed_visits==1
        assert result.conversion_percent==100
        assert all(step.count==1 for step in result.steps)
        assert all(change.drop_off_count==0 for change in result.transitions)
        assert (await session.scalar(select(func.count()).select_from(Event)))==8
        row=(await session.execute(select(Event).limit(1))).scalar_one()
        assert len(row.attempt_hash)==64 and row.attempt_hash!=state.payload_json["telegram_funnel_attempt_id"]
        assert set(Event.__table__.columns.keys())=={"id","attempt_hash","step","occurred_at","booking_id"}
        item.archived_at=AT
        await session.commit()
        archived=await aggregate(session)
        assert archived.succeeded==archived.completed_visits==1
        item.status=BookingStatus.cancelled
        await session.commit()
        result=await aggregate(session)
        assert result.succeeded==1 and result.completed_visits==0


async def test_missing_out_of_order_and_outside_cohort_cannot_inflate_conversion(database):
    async with database() as session:
        await emit(session,bot(),("master_selected",))  # No start.
        await emit(session,bot(),("booking_start","master_selected"))  # Missing contact.
        await emit(session,bot(),("contact_entered","booking_start","master_selected"))  # Contact before start.
        await emit(session,bot(),("booking_start","contact_entered"),at=AT-timedelta(days=1))
        result=await aggregate(session)
        assert [s.count for s in result.steps]==[2,0,0,0,0,0,0,0]
        assert result.transitions[0].drop_off_count==2
        assert result.succeeded==0
        previous=await aggregate(session,date(2026,10,11))
        assert previous.started==1 and previous.steps[1].count==1


async def test_cohort_keeps_later_milestones_and_context_once(database):
    async with database() as session:
        state=bot()
        await emit(session,state,("booking_start",))
        await emit(session,state,TELEGRAM_FUNNEL_STEPS[1:-1],at=AT+timedelta(days=2))
        for step in ("no_slot","stale_schedule","booking_error","draft_cancelled"):
            assert await record_telegram_step(session,state,step)
            assert not await record_telegram_step(session,state,step)
        await session.commit()
        result=await aggregate(session)
        assert [s.count for s in result.steps]==[1,1,1,1,1,1,1,0]
        assert all(s.count==1 for s in result.contextual_steps)
        assert (await aggregate(session,date(2026,10,14))).started==0


@pytest.mark.parametrize("payload,step,booking_id", [({},"booking_start",None),({"telegram_funnel_attempt_id":"phone"},"booking_start",None),({"telegram_funnel_attempt_id":None},"booking_start",None),({"telegram_funnel_attempt_id":str(uuid4())},"unknown",None),({"telegram_funnel_attempt_id":str(uuid4())},"booking_success",None),({"telegram_funnel_attempt_id":str(uuid4())},"contact_entered",42)])
async def test_invalid_inputs_do_not_write(database,payload,step,booking_id):
    async with database() as session:
        assert not await record_telegram_step(session,SimpleNamespace(payload_json=payload),step,booking_id=booking_id)
        assert await session.scalar(select(func.count()).select_from(Event))==0


async def test_success_requires_actual_telegram_booking_and_is_unique_across_attempts(database):
    async with database() as session:
        web=await booking(session,BookingSource.web)
        assert not await record_telegram_step(session,bot(),"booking_success",booking_id=web.id)
        assert not await record_telegram_step(session,bot(),"booking_success",booking_id=999999)
        item=await booking(session)
        assert await record_telegram_step(session,bot(),"booking_success",booking_id=item.id)
        assert not await record_telegram_step(session,bot(),"booking_success",booking_id=item.id)
        await session.commit()
        assert (await aggregate(session)).succeeded==0  # Success alone has no started cohort.


async def test_telemetry_database_failure_preserves_caller_booking(database):
    async with database() as session:
        await session.execute(text("CREATE FUNCTION reject_tg_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic'; END; $$"))
        await session.execute(text("CREATE TRIGGER reject_tg_event BEFORE INSERT ON telegram_booking_funnel_events FOR EACH ROW EXECUTE FUNCTION reject_tg_event()"))
        item=await booking(session)
        assert not await record_telegram_step(session,bot(),"booking_success",booking_id=item.id)
        await session.commit()
        identifier=item.id
    async with database() as session:
        assert await session.get(Booking,identifier) is not None
        assert await session.scalar(select(func.count()).select_from(Event))==0


async def test_empty_and_invalid_ranges_and_admin_authorization(database):
    from app.api.v1.routes.statistics import get_telegram_booking_funnel
    async with database() as session:
        result=await aggregate(session)
        assert result.started==result.succeeded==result.completed_visits==0
        assert result.conversion_percent is None
        assert all(t.conversion_percent is None for t in result.transitions)
        for start,end in [(date(2026,10,12),date(2026,10,11)),(date(2025,1,1),date(2026,1,2))]:
            with pytest.raises(HTTPException) as error:
                await aggregate_telegram_funnel(session,date_from=start,date_to=end)
            assert error.value.status_code==422
        with pytest.raises(HTTPException) as error:
            await get_telegram_booking_funnel(date_from=date(2026,10,12),date_to=date(2026,10,12),current_user=SimpleNamespace(is_superuser=False),session=session)
        assert error.value.status_code==403
        allowed=await get_telegram_booking_funnel(date_from=date(2026,10,12),date_to=date(2026,10,12),current_user=SimpleNamespace(is_superuser=True),session=session)
        assert allowed.started==0


async def test_migration_upgrade_downgrade_preserves_booking(database):
    path=Path(__file__).parents[1]/"alembic/versions/0109_telegram_booking_funnel.py"
    spec=importlib.util.spec_from_file_location("tg_funnel_migration",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision=="0108_booking_archive"
    async with database() as session:
        item=await booking(session)
        identifier=item.id
        await session.commit()
        connection=await session.connection()
        def roundtrip(sync):
            def preserved():
                row = sync.execute(text("SELECT id, customer_name, customer_phone, source FROM bookings WHERE id=:id"), {"id": identifier}).one()
                assert tuple(row) == (identifier, "Synthetic", "synthetic", "telegram")
            Event.__table__.drop(sync)
            with Operations.context(MigrationContext.configure(sync)):
                module.upgrade()
                preserved()
                columns=[r[0] for r in sync.execute(text("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='telegram_booking_funnel_events'"))]
                assert set(columns)==set(Event.__table__.columns.keys())
                module.downgrade()
                assert sync.scalar(text("SELECT to_regclass('telegram_booking_funnel_events')")) is None
                preserved()
                module.upgrade()
                preserved()
        await connection.run_sync(roundtrip)
        await session.commit()
        assert await session.get(Booking,identifier) is not None
        assert await record_telegram_step(session,bot(),"booking_start")


async def test_archived_cohort_counts_are_explicit_under_operational_session(database):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.core.database import OperationalSession
    async with database() as session:
        item = await booking(session, status=BookingStatus.completed)
        await emit(session, bot(), TELEGRAM_FUNNEL_STEPS, item)
        item.archived_at = AT
        await session.commit()
    sessions = async_sessionmaker(database.kw["bind"], sync_session_class=OperationalSession,
                                 expire_on_commit=False)
    async with sessions() as session:
        assert await session.scalar(select(func.count(Booking.id))) == 0
        result = await aggregate(session)
        assert result.created_bookings == result.succeeded == result.completed_visits == 1


async def test_parallel_replays_persist_single_milestone(database):
    import asyncio
    state=bot()
    async def write():
        async with database() as session:
            recorded=await record_telegram_step(session,state,"booking_start")
            await session.commit()
            return recorded
    assert sorted(await asyncio.gather(write(),write(),write()))==[False,False,True]
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Event))==1


async def test_kyiv_midnight_and_inclusive_366_day_boundaries(database):
    async with database() as session:
        await emit(session,bot(),("booking_start",),at=datetime(2026,10,11,20,59,59,tzinfo=UTC))
        await emit(session,bot(),("booking_start",),at=datetime(2026,10,11,21,0,0,tzinfo=UTC))
        await emit(session,bot(),("booking_start",),at=datetime(2026,10,12,21,0,0,tzinfo=UTC))
        assert (await aggregate(session)).started==1
        assert (await aggregate(session,date(2026,10,11))).started==1
        assert (await aggregate(session,date(2026,10,13))).started==1
        result=await aggregate_telegram_funnel(session,date_from=date(2026,1,1),date_to=date(2027,1,1))
        assert result.started==3


async def test_missing_telemetry_keeps_actual_created_and_completed_counts(database):
    async with database() as session:
        item=await booking(session,status=BookingStatus.completed)
        state=bot()
        await emit(session,state,("booking_start","contact_entered","booking_success"),item)
        result=await aggregate(session)
        assert result.started==1
        assert result.succeeded==0
        assert result.created_bookings==result.tracking_gap_successes==result.completed_visits==1
        assert result.conversion_percent==0
