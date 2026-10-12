from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import select
from app.models.booking import Booking, BookingStatus, MasterAvailabilityWindow
from app.models.messaging import TelegramBotSession
from app.services import telegram_booking_management as management
from app.api.v1.routes import messaging as routes
from test_inventory_integration import database, anyio_backend  # noqa: F401
from test_telegram_booking_flow import seed
from test_telegram_connect import FakeTelegramMessageProvider


@pytest.fixture(autouse=True)
def replies(monkeypatch):
    from app.services import telegram_inbox
    async def noop(*a, **kw):
        return None
    monkeypatch.setattr(telegram_inbox, 'send_event_reply', noop)
    FakeTelegramMessageProvider.sent=[]
    FakeTelegramMessageProvider.answered_callbacks=[]


async def setup(session):
    customer,master,other,services,contact=await seed(session)
    day=datetime.now(routes.KYIV_TZ).date()+timedelta(days=2)
    while management.service.is_closed_business_day(day):
        day+=timedelta(days=1)
    old=datetime.combine(day,datetime.min.time(),tzinfo=routes.KYIV_TZ)+timedelta(hours=10)
    booking=Booking(customer_id=customer.id, master_id=master.id, service_id=services[0].id,
        customer_name='Client', customer_phone=customer.phone, start_at=old,end_at=old+timedelta(minutes=45),
        status=BookingStatus.confirmed,total_amount=650)
    bot=TelegramBotSession(chat_id='123',linked_customer_id=customer.id,telegram_contact_id=contact.id,
        state='booked',payload_json={'booking_id': 99})
    session.add_all([booking,bot,MasterAvailabilityWindow(master_id=master.id,
        start_at=old-timedelta(minutes=30),end_at=old+timedelta(hours=5))])
    await session.commit()
    return booking,bot,contact,other,old


async def call(session,contact,text):
    return await management.dispatch_telegram_booking_management(FakeTelegramMessageProvider(),session,
        chat_id='123',text=text,telegram_contact=contact,callback_query_id=None)


@pytest.mark.anyio
@pytest.mark.parametrize('keep',[False,True])
async def test_cancel_requires_explicit_current_confirmation(database,keep):
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        assert (await call(session,contact,f'cancel_booking:{booking.id}'))['action']=='booking_management_prompt'
        assert booking.status==BookingStatus.confirmed
        token=bot.payload_json['booking_management']['token']
        assert any(b['text']=='Перенести замість скасування' for row in FakeTelegramMessageProvider.sent[-1][2]['inline_keyboard'] for b in row)
        assert (await call(session,contact,'manage_cancel:forged'))['action']=='booking_management_stale'
        assert booking.status==BookingStatus.confirmed
        await call(session,contact,('manage_keep:' if keep else 'manage_cancel:')+token)
        assert booking.status==(BookingStatus.confirmed if keep else BookingStatus.cancelled)
        assert booking.start_at==old
        assert 'booking_management' not in bot.payload_json
        assert (await call(session,contact,'manage_cancel:'+token))['action']=='booking_management_stale'


@pytest.mark.anyio
async def test_foreign_customer_cannot_manage_booking(database):
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        contact.linked_customer_id=None
        contact.phone='+380501119999'
        bot.linked_customer_id=None
        await session.commit()
        assert (await call(session,contact,f'reschedule_booking:{booking.id}'))['action']=='booking_management_unavailable'
        assert booking.status==BookingStatus.confirmed and booking.start_at==old
        assert 'booking_management' not in bot.payload_json


@pytest.mark.anyio
@pytest.mark.parametrize('identifier', ['0', '-1', '2147483648', '9999999999999999999'])
async def test_invalid_booking_id_is_rejected_before_booking_query(monkeypatch, identifier):
    async def get_session(*args):
        return None
    async def forbidden(*args):
        pytest.fail('Invalid INTEGER identifier reached a booking query')
    monkeypatch.setattr(routes, '_get_telegram_bot_session', get_session)
    monkeypatch.setattr(management, '_owned_booking', forbidden)
    response = await call(object(), None, 'cancel_booking:' + identifier)
    assert response['action'] == 'booking_management_stale'


@pytest.mark.anyio
async def test_archived_future_booking_cannot_be_managed_under_operational_session(database):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.core.database import OperationalSession
    async with database() as session:
        booking, _, _, _, old = await setup(session)
        booking.archived_at = datetime.now(UTC)
        identifier = booking.id
        await session.commit()
    sessions = async_sessionmaker(database.kw['bind'], sync_session_class=OperationalSession,
                                 expire_on_commit=False)
    async with sessions() as session:
        contact = await session.scalar(select(routes.TelegramContact))
        result = await call(session, contact, f'cancel_booking:{identifier}')
        assert result['action'] == 'booking_management_unavailable'
        bot = await session.scalar(select(TelegramBotSession))
        assert 'booking_management' not in bot.payload_json
    async with database() as session:
        booking = await session.get(Booking, identifier)
        assert booking.status == BookingStatus.confirmed and booking.start_at == old


@pytest.mark.anyio
@pytest.mark.parametrize('conflict',[False,True])
async def test_atomic_reschedule_keeps_original_until_confirmed_and_preserves_price(database,conflict):
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        await call(session,contact,f'reschedule_booking:{booking.id}')
        data=bot.payload_json['booking_management']
        await call(session,contact,f'manage_date:{data["slot_token"]}:{old:%Y%m%d}')
        data=bot.payload_json['booking_management']
        index=next(i for i,v in enumerate(data['slots']) if datetime.fromisoformat(v).hour==12)
        await call(session,contact,f'manage_slot:{data["slot_token"]}:{index}')
        chosen=bot.payload_json['booking_management']; token=chosen['confirmation']
        assert booking.start_at==old and booking.total_amount==650
        new=datetime.fromisoformat(chosen['chosen_start'])
        if conflict:
            session.add(Booking(customer_id=booking.customer_id,master_id=booking.master_id,service_id=booking.service_id,
                customer_name='Other',customer_phone='+380501119999',start_at=new,end_at=new+timedelta(minutes=45)))
            await session.commit()
        response=await call(session,contact,'manage_confirm:'+token)
        await session.refresh(booking)
        assert booking.status==BookingStatus.confirmed and booking.total_amount==650
        assert booking.end_at.astimezone(UTC)-booking.start_at.astimezone(UTC)==timedelta(minutes=45)
        assert booking.start_at==(old if conflict else new)
        assert response['action']==('reschedule_conflict' if conflict else 'booking_management_done')
        assert (await call(session,contact,'manage_confirm:'+token))['action']=='booking_management_stale'


@pytest.mark.anyio
async def test_admin_change_invalidates_confirmation_and_other_day_buttons(database):
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        await call(session,contact,f'cancel_booking:{booking.id}')
        token=bot.payload_json['booking_management']['token']
        booking.start_at+=timedelta(minutes=15)
        booking.end_at+=timedelta(minutes=15)
        await session.commit()
        assert (await call(session,contact,'manage_cancel:'+token))['action']=='booking_management_stale'
        assert booking.status==BookingStatus.confirmed
        await call(session,contact,f'reschedule_booking:{booking.id}')
        token=bot.payload_json['booking_management']['slot_token']
        await call(session,contact,'manage_next:'+token)
        assert (await call(session,contact,f'manage_date:{token}:{old:%Y%m%d}'))['action']=='booking_management_stale'


@pytest.mark.anyio
@pytest.mark.parametrize('status,past',[(BookingStatus.completed,False),(BookingStatus.cancelled,False),(BookingStatus.confirmed,True)])
async def test_inactive_or_started_booking_cannot_be_changed(database,status,past):
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        booking.status=status
        if past:
            booking.start_at=datetime.now(UTC)-timedelta(minutes=5)
        await session.commit()
        assert (await call(session,contact,f'cancel_booking:{booking.id}'))['action']=='booking_management_unavailable'
        assert booking.status==status


@pytest.mark.anyio
async def test_reschedule_preview_excludes_own_occupied_interval(database):
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        starts=await management._slots(session,booking,old.date())
        assert old+timedelta(minutes=15) in starts
        assert old in starts


@pytest.mark.anyio
@pytest.mark.parametrize('when',['picker','confirmation'])
async def test_master_disabled_during_reschedule_recovers_without_mutation(database,when):
    from app.models.booking import Master
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        await call(session,contact,f'reschedule_booking:{booking.id}')
        data=bot.payload_json['booking_management']
        date_callback=f'manage_date:{data["slot_token"]}:{old:%Y%m%d}'
        if when=='confirmation':
            await call(session,contact,date_callback)
            data=bot.payload_json['booking_management']
            await call(session,contact,f'manage_slot:{data["slot_token"]}:0')
            callback='manage_confirm:'+bot.payload_json['booking_management']['confirmation']
        else:
            callback=date_callback
        (await session.get(Master,booking.master_id)).is_active=False
        await session.commit()
        await call(session,contact,callback)
        assert booking.start_at==old and booking.status==BookingStatus.confirmed
        assert bot.payload_json['booking_management']['slots']==[]
        assert any(b['text']=='Залишити поточний запис' for row in FakeTelegramMessageProvider.sent[-1][2]['inline_keyboard'] for b in row)


@pytest.mark.anyio
async def test_confirmation_refreshes_master_disabled_in_another_transaction(database):
    from sqlalchemy import update
    from app.models.booking import Master
    async with database() as session:
        booking,bot,contact,_,old=await setup(session)
        await call(session,contact,f'reschedule_booking:{booking.id}')
        data=bot.payload_json['booking_management']
        await call(session,contact,f'manage_date:{data["slot_token"]}:{old:%Y%m%d}')
        data=bot.payload_json['booking_management']
        await call(session,contact,f'manage_slot:{data["slot_token"]}:0')
        token=bot.payload_json['booking_management']['confirmation']
        cached=await session.get(Master,booking.master_id)
        assert cached.is_active
        async with database() as other:
            await other.execute(update(Master).where(Master.id==booking.master_id).values(is_active=False))
            await other.commit()
        assert cached.is_active  # Identity map still has the old state.
        assert (await call(session,contact,'manage_confirm:'+token))['action']=='reschedule_conflict'
        assert not cached.is_active
        assert booking.start_at==old and booking.status==BookingStatus.confirmed
