"""Telegram guided flow contracts against real PostgreSQL state."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.v1.routes import messaging as routes
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingSource, Master
from app.models.customer import Customer
from app.models.messaging import TelegramBotSession, TelegramContact
from app.models.telegram_booking_funnel import TelegramBookingFunnelEvent
from test_inventory_integration import database, anyio_backend  # noqa: F401
from test_telegram_connect import FakeTelegramMessageProvider


@pytest.fixture(autouse=True)
def isolated_delivery(monkeypatch):
    from app.services import telegram_inbox
    async def reply(*args, **kwargs):
        return None
    monkeypatch.setattr(telegram_inbox, 'send_event_reply', reply)
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.sent_photos = []
    FakeTelegramMessageProvider.answered_callbacks = []


def callbacks(markup):
    return [item['callback_data'] for row in markup.get('inline_keyboard', []) for item in row]


@pytest.mark.parametrize('ids,total,duration', [([],0,0),([1],500,30),([2],0,15),([1,2],500,45)])
def test_service_snapshot_shows_selection_total_duration_and_gated_next(ids,total,duration):
    services = [BarberService(id=1,name='Hair',price=500,duration_minutes=30),
                BarberService(id=2,name='Beard',price=0,duration_minutes=15)]
    body = routes._services_selection_message(services,ids)
    assert f'Загальна вартість: {total} грн' in body
    assert f'Тривалість: {duration} хв' in body
    markup = routes._services_reply_markup(services,ids)
    assert ('Далі' in callbacks(markup)) == bool(ids)
    for row, service in zip(markup['inline_keyboard'],services):
        assert ('✅' in row[0]['text']) == (service.id in ids)
        assert (f'✓ {service.name}' in body) == (service.id in ids)


def test_preview_contains_each_service_and_end_time_across_midnight():
    master = Master(full_name='Майстер Петро')
    services = [BarberService(name='Hair',price=500,duration_minutes=45),
                BarberService(name='Beard',price=300,duration_minutes=30)]
    body = routes._booking_details_message(master,services,datetime(2026,11,12,23,30,tzinfo=routes.KYIV_TZ))
    for fragment in ('Майстер Петро','Hair','Beard','500 грн','300 грн','800 грн','75 хв','23:30','00:45','12 листопада'):
        assert fragment in body
    assert set(callbacks(routes._ready_to_book_reply_markup('token'))) >= {
        'confirm_booking:token','Майстер','Послуги','Обрати іншу дату'}


async def seed(session):
    customer = Customer(phone='+380501112233',name='Client')
    master = Master(full_name='Майстер Петро',is_active=True)
    other = Master(full_name='Інший Майстер',is_active=True)
    session.add_all([customer,master,other])
    await session.flush()
    services = [BarberService(master_id=master.id,name='Hair',price=500,duration_minutes=30,is_active=True),
                BarberService(master_id=master.id,name='Beard',price=300,duration_minutes=15,is_active=True)]
    contact = TelegramContact(chat_id='123',phone=customer.phone,linked_customer_id=customer.id,first_name='Client')
    session.add_all([*services,contact])
    await session.commit()
    return customer,master,other,services,contact


async def start(session, contact):
    assert await routes._handle_new_booking_action(FakeTelegramMessageProvider(),session,
        chat_id='123',telegram_contact=contact,callback_query_id='start')
    return (await session.execute(select(TelegramBotSession))).scalar_one()


@pytest.mark.anyio
@pytest.mark.parametrize('saved_total',[800,650,0])
async def test_guided_roundtrip_persisted_card_and_deduplicated_funnel(database,monkeypatch,saved_total):
    telegram = FakeTelegramMessageProvider()
    visit_date = date(2026,11,12)
    start_at = datetime(2026,11,12,10,tzinfo=routes.KYIV_TZ)
    async def dates(*args,**kwargs):
        return [visit_date]
    async def slots(*args,**kwargs):
        return [SimpleNamespace(start_at=start_at)]
    monkeypatch.setattr(routes,'_available_visit_dates',dates)
    monkeypatch.setattr(routes,'_available_visit_slots',slots)
    async with database() as session:
        customer,master,other,services,contact = await seed(session)
        bot = await start(session,contact)
        attempt = bot.payload_json['telegram_funnel_attempt_id']
        assert any(f'select_master:{master.id}' in callbacks(message[2])
                   for message in telegram.sent if len(message)>2)
        assert await routes._handle_master_selection(telegram,session,chat_id='123',master_id=master.id,
            telegram_contact=contact,callback_query_id='master')
        assert bot.state == routes.TELEGRAM_SELECTING_SERVICES_STATE
        assert 'Далі' not in callbacks(telegram.sent[-1][2])
        for service in services:
            assert await routes._handle_service_selection(telegram,session,chat_id='123',
                service_id=service.id,callback_query_id='service')
        assert '800 грн' in telegram.sent[-1][1] and '45 хв' in telegram.sent[-1][1]
        assert 'Далі' in callbacks(telegram.sent[-1][2])
        assert await routes._handle_date_time_action(telegram,session,chat_id='123')
        assert await routes._handle_date_selection(telegram,session,chat_id='123',visit_date=visit_date,callback_query_id='date')
        slot_token = bot.payload_json['slot_token']
        assert await routes._handle_time_selection(telegram,session,chat_id='123',visit_time=start_at,
            callback_query_id='time',slot_token=slot_token)
        body = telegram.sent[-1][1]
        assert all(fragment in body for fragment in ('Майстер Петро','Hair','Beard','800 грн','45 хв','10:00','10:45','12 листопада'))
        token = bot.payload_json['confirmation_token']
        assert f'confirm_booking:{token}' in callbacks(telegram.sent[-1][2])
        created = []
        async def create_booking(db,payload,**kwargs):
            assert payload.service_ids == [service.id for service in services]
            assert payload.start_at == start_at and kwargs['commit'] is False
            booking = Booking(master_id=master.id,service_id=services[0].id,customer_id=customer.id,
                customer_name='Client',customer_phone=customer.phone,start_at=start_at,
                end_at=start_at+timedelta(minutes=45),total_amount=saved_total,source=BookingSource.telegram,
                service_items=[BookingServiceItem(service_id=service.id,price_amount=service.price,position=index)
                               for index,service in enumerate(services)])
            db.add(booking)
            await db.flush()
            for service in services:
                service.price = 2500
            await db.flush()
            created.append(booking.id)
            return booking
        monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',create_booking)
        assert await routes._handle_booking_confirmation(telegram,session,chat_id='123',telegram_contact=contact,
            background_tasks=None,confirmation_token=token)
        success = telegram.sent[-1]
        assert f'Загальна вартість: {saved_total} грн' in success[1]
        assert '(500 грн)' in success[1] and '(300 грн)' in success[1]
        assert '2500' not in success[1]
        assert '10:45' in success[1]
        assert success[2] == routes._after_booking_reply_markup()
        assert await routes._handle_booking_confirmation(telegram,session,chat_id='123',telegram_contact=contact,
            background_tasks=None,confirmation_token=token)
        assert len(created)==1
        events = (await session.execute(select(TelegramBookingFunnelEvent))).scalars().all()
        expected = {'booking_start','contact_entered','master_selected','service_selected','date_selected',
                    'slot_selected','confirmation_viewed','booking_success'}
        assert {event.step for event in events} == expected
        assert len(events)==len(expected)
        assert len({event.attempt_hash for event in events})==1
        for event in events:
            assert len(event.attempt_hash)==64
            assert customer.phone not in repr(event.__dict__) and 'Client' not in repr(event.__dict__)
        bot = await start(session,contact)
        assert bot.payload_json['telegram_funnel_attempt_id'] != attempt
        events = (await session.execute(select(TelegramBookingFunnelEvent))).scalars().all()
        assert len({event.attempt_hash for event in events})==2
        assert len(events)==len(expected)+2


@pytest.mark.anyio
@pytest.mark.parametrize('edit',['remove_last','add_service','same_master','other_master'])
async def test_edit_invalidates_dependencies_keeps_attempt_and_empty_selection_blocks_next(database,monkeypatch,edit):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        _,master,other,services,contact = await seed(session)
        bot = await start(session,contact)
        attempt = bot.payload_json['telegram_funnel_attempt_id']
        assert await routes._handle_master_selection(telegram,session,chat_id='123',master_id=master.id,
            telegram_contact=contact,callback_query_id='master')
        await routes._handle_service_selection(telegram,session,chat_id='123',service_id=services[0].id,callback_query_id='service')
        bot.payload_json = {**bot.payload_json,'selected_visit_date':'2026-11-12',
            'selected_visit_time':'2026-11-12T10:00:00+02:00','slot_token':'old','confirmation_token':'old','booking_id':999}
        bot.state = routes.TELEGRAM_READY_TO_BOOK_STATE
        await session.commit()
        if edit in {'same_master','other_master'}:
            await routes._handle_master_selection(telegram,session,chat_id='123',
                master_id=master.id if edit=='same_master' else other.id,telegram_contact=contact,callback_query_id='edit')
            assert bot.selected_service_id is None
        else:
            await routes._handle_service_selection(telegram,session,chat_id='123',
                service_id=services[0].id if edit=='remove_last' else services[1].id,callback_query_id='edit')
        assert bot.payload_json['telegram_funnel_attempt_id']==attempt
        for key in ('selected_visit_date','selected_visit_time','slot_token','confirmation_token','booking_id'):
            assert key not in bot.payload_json
        async def forbidden(*args,**kwargs):
            pytest.fail('Old token created booking')
        monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',forbidden)
        assert not await routes._handle_booking_confirmation(telegram,session,chat_id='123',telegram_contact=contact,
            background_tasks=None,confirmation_token='old')
        if edit=='remove_last':
            assert not bot.selected_service_id
            assert 'Далі' not in callbacks(telegram.sent[-2][2])
            assert not await routes._handle_date_time_action(telegram,session,chat_id='123')


def test_saved_card_duration_uses_elapsed_minutes_across_dst_change():
    from datetime import timezone
    booking = SimpleNamespace(
        master=SimpleNamespace(full_name='Петро', full_name_uk='Петро'),
        redirected_from_master=None, service_items=[], service=None,
        service_prices={}, total_amount=0, customer_comment='',
        start_at=datetime(2026,10,25,0,30,tzinfo=timezone.utc),
        end_at=datetime(2026,10,25,2,30,tzinfo=timezone.utc),
    )
    assert 'Тривалість: 120 хв' in routes._booking_view_message(booking)
