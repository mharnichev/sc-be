"""Draft-bound recovery, explicit SMS waitlist consent and live slot refresh."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.v1.routes import messaging as routes
from app.models.booking import BaseService, BarberService, Booking, BookingSource, Master, MasterAvailabilityWindow
from app.models.customer import Customer
from app.models.messaging import TelegramBotSession, TelegramContact
from app.models.waitlist import WaitlistRequest
from app.services.telegram_booking_recovery import recovery_context, recovery_is_current
from test_inventory_integration import database, anyio_backend  # noqa: F401
from test_telegram_connect import FakeDateTimeSession, FakeTelegramMessageProvider


@pytest.fixture(autouse=True)
def isolated_replies(monkeypatch):
    from app.services import telegram_inbox
    async def reply(*args, **kwargs):
        return None
    monkeypatch.setattr(telegram_inbox, 'send_event_reply', reply)
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []


def callbacks(markup):
    return [button['callback_data'] for row in (markup or {}).get('inline_keyboard', []) for button in row]


def future_day():
    day = datetime.now(routes.KYIV_TZ).date() + timedelta(days=1)
    while day.weekday() == 0:
        day += timedelta(days=1)
    return day


def local_time(day, hour=10):
    return datetime.combine(day, datetime.min.time(), routes.KYIV_TZ).replace(hour=hour)


def draft(day):
    bot = SimpleNamespace(selected_master_id=1, selected_service_id=10,
        state=routes.TELEGRAM_SELECTING_TIME_STATE,
        payload_json={'selected_service_ids': [10], 'selected_visit_date': day.isoformat(),
                      'telegram_funnel_attempt_id': 'attempt'})
    bot.payload_json['booking_recovery'] = {
        'token': 'a'*16, 'context': recovery_context(bot, day), 'offers': []}
    return bot


@pytest.mark.parametrize('change', ['master', 'services', 'date', 'attempt', 'state', 'token'])
def test_recovery_rejects_actual_draft_changes(change):
    bot = draft(future_day())
    if change == 'master':
        bot.selected_master_id = 2
    elif change == 'services':
        bot.payload_json['selected_service_ids'] = [20]
    elif change == 'date':
        bot.payload_json['selected_visit_date'] = (future_day()+timedelta(days=1)).isoformat()
    elif change == 'attempt':
        bot.payload_json['telegram_funnel_attempt_id'] = 'next-attempt'
    elif change == 'state':
        bot.state = routes.TELEGRAM_BOOKED_STATE
    assert not recovery_is_current(bot, 'b'*16 if change == 'token' else 'a'*16)


@pytest.mark.anyio
async def test_date_next_page_covers_full_horizon_and_rejects_old_page(monkeypatch):
    days = [future_day()+timedelta(days=n) for n in range(32)]
    bot = draft(days[0])
    session = FakeDateTimeSession(bot)
    async def available(*args, **kwargs):
        return days
    monkeypatch.setattr(routes, '_available_visit_dates', available)
    telegram = FakeTelegramMessageProvider()
    assert await routes._handle_date_time_action(telegram, session, chat_id='123')
    seen = []
    old_next = None
    while True:
        buttons = callbacks(telegram.sent[-1][2])
        seen.extend(button.removeprefix('select_date:') for button in buttons if button.startswith('select_date:'))
        next_button = next((button for button in buttons if button.startswith('dates_page:')), None)
        if next_button is None:
            break
        if old_next is None:
            old_next = next_button
        assert await routes._handle_visit_date_page_callback(telegram, session,
            chat_id='123', text=next_button, callback_query_id='next')
    assert seen == [day.isoformat() for day in days]
    assert not await routes._handle_visit_date_page_callback(telegram, session,
        chat_id='123', text=old_next, callback_query_id='old')
    bot.payload_json['selected_service_ids'] = [20]
    assert not await routes._handle_visit_date_page_callback(telegram, session,
        chat_id='123', text=old_next, callback_query_id='changed')


async def seed(session, *, windows=True):
    day = future_day()
    later = day+timedelta(days=1)
    while later.weekday() == 0:
        later += timedelta(days=1)
    customer = Customer(phone='+380501112233', name='Client')
    master = Master(full_name='Original Master', is_active=True, show_on_master_block=True)
    other = Master(full_name='Other Master', is_active=True, show_on_master_block=True)
    catalog_service = BaseService(name='Hair', title_uk='Hair', duration_minutes=30, price=500, is_active=True)
    session.add_all([customer, master, other, catalog_service])
    await session.flush()
    original_service = BarberService(master_id=master.id, base_service_id=catalog_service.id, name='Hair', price=500,
                                     duration_minutes=30, is_active=True)
    other_service = BarberService(master_id=other.id, base_service_id=catalog_service.id, name='Hair', price=700,
                                  duration_minutes=30, is_active=True)
    contact = TelegramContact(chat_id='123', telegram_user_id='555', phone=customer.phone,
                              linked_customer_id=customer.id, first_name='Client')
    session.add_all([original_service, other_service, contact])
    await session.flush()
    bot = TelegramBotSession(chat_id='123', telegram_contact_id=contact.id,
        linked_customer_id=customer.id, selected_master_id=master.id,
        selected_service_id=original_service.id, state=routes.TELEGRAM_SELECTING_TIME_STATE,
        payload_json={'selected_service_ids': [original_service.id], 'selected_visit_date': day.isoformat(),
                      'telegram_funnel_attempt_id': 'recovery-attempt'})
    session.add(bot)
    if windows:
        session.add_all([
            MasterAvailabilityWindow(master_id=master.id, start_at=local_time(later), end_at=local_time(later, 11)),
            MasterAvailabilityWindow(master_id=other.id, start_at=local_time(day), end_at=local_time(day, 11)),
        ])
    await session.commit()
    return day, later, customer, master, other, original_service, other_service, contact, bot


@pytest.mark.anyio
@pytest.mark.parametrize('pick_other', [False, True])
async def test_no_slots_recovers_nearest_same_or_explicit_equivalent_other_master(database, pick_other):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        day, later, _, master, other, original_service, other_service, contact, bot = await seed(session)
        assert await routes._handle_date_selection(telegram, session, chat_id='123',
                                                    visit_date=day, callback_query_id='date')
        recovery = bot.payload_json['booking_recovery']
        offers = recovery['offers']
        target_master = other if pick_other else master
        index = next(index for index, offer in enumerate(offers) if offer['master']['id'] == target_master.id)
        assert offers[index]['date'] == (day if pick_other else later).isoformat()
        expected_service = other_service if pick_other else original_service
        assert offers[index]['service_ids'] == [expected_service.id]
        button = f"recover_slot:{recovery['token']}:{index}"
        assert button in callbacks(telegram.sent[-1][2])
        assert await routes._handle_booking_recovery_callback(telegram, session, chat_id='123',
            text=button, telegram_contact=contact, callback_query_id='alternative')
        assert bot.selected_master_id == target_master.id
        assert bot.payload_json['selected_service_ids'] == [expected_service.id]
        assert bot.payload_json['selected_visit_date'] == (day if pick_other else later).isoformat()
        assert bot.state == routes.TELEGRAM_READY_TO_BOOK_STATE
        assert 'booking_recovery' not in bot.payload_json
        assert not await routes._handle_booking_recovery_callback(telegram, session, chat_id='123',
            text=button, telegram_contact=contact, callback_query_id='reused')
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize('deactivate', ['master', 'service'])
async def test_removed_master_or_service_between_date_pages_resets_dependent_choices(database, deactivate):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        day, _, _, master, _, service, _, _, bot = await seed(session, windows=False)
        days = []
        while len(days) < 18:
            if day.weekday() != 0:
                days.append(day)
                session.add(MasterAvailabilityWindow(master_id=master.id,
                    start_at=local_time(day), end_at=local_time(day, 11)))
            day += timedelta(days=1)
        await session.commit()
        assert await routes._handle_date_time_action(telegram, session, chat_id='123')
        next_button = next(button for button in callbacks(telegram.sent[-1][2])
                           if button.startswith('dates_page:'))
        assert bot.state == routes.TELEGRAM_SELECTING_DATE_STATE
        bot.payload_json = {**bot.payload_json, 'selected_visit_time': local_time(days[0]).isoformat(),
                            'confirmation_token': 'old-confirmation', 'slot_token': 'old-slot'}
        (master if deactivate == 'master' else service).is_active = False
        await session.commit()
        assert not await routes._handle_visit_date_page_callback(telegram, session,
            chat_id='123', text=next_button, callback_query_id='removed')
        assert bot.state == routes.TELEGRAM_SELECTING_SERVICES_STATE
        for key in ('date_page', 'selected_visit_time', 'confirmation_token', 'slot_token'):
            assert key not in bot.payload_json
        assert 'Майстер або послуги вже недоступні' in telegram.sent[-1][1]
        assert telegram.sent[-1][2] == routes._booking_action_reply_markup()
        assert not await routes._handle_visit_date_page_callback(telegram, session,
            chat_id='123', text=next_button, callback_query_id='retry')
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize('deactivate', ['master', 'service'])
async def test_removed_other_master_or_service_refreshes_recovery_without_switching_draft(database, deactivate):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        day, _, _, master, other, original_service, other_service, contact, bot = await seed(session)
        assert await routes._handle_date_selection(telegram, session, chat_id='123',
                                                    visit_date=day, callback_query_id='date')
        recovery = bot.payload_json['booking_recovery']
        index = next(index for index, offer in enumerate(recovery['offers']) if offer['master']['id'] == other.id)
        (other if deactivate == 'master' else other_service).is_active = False
        await session.commit()
        assert not await routes._handle_booking_recovery_callback(telegram, session, chat_id='123',
            text=f"recover_slot:{recovery['token']}:{index}", telegram_contact=contact, callback_query_id='removed')
        assert bot.selected_master_id == master.id
        assert bot.payload_json['selected_service_ids'] == [original_service.id]
        assert bot.payload_json['selected_visit_date'] == day.isoformat()
        assert bot.payload_json['booking_recovery']['token'] != recovery['token']
        assert all(offer['master']['id'] != other.id for offer in bot.payload_json['booking_recovery']['offers'])
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0


@pytest.mark.anyio
async def test_waitlist_requires_explicit_sms_consent_and_is_duplicate_friendly(database, monkeypatch):
    secret = 'private-cancel-capability-must-not-be-shown'
    monkeypatch.setattr(routes.telegram_waitlist_service, '_new_token', lambda: secret)
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        day, _, _, master, _, original_service, _, contact, bot = await seed(session, windows=False)
        assert await routes._handle_date_selection(telegram, session, chat_id='123', visit_date=day, callback_query_id='date')
        token = bot.payload_json['booking_recovery']['token']
        async def action(name):
            return await routes._handle_booking_recovery_callback(telegram, session, chat_id='123',
                text=f'{name}:{token}', telegram_contact=contact, callback_query_id='consent')
        assert not await action('recover_waitlist_confirm')
        assert await session.scalar(select(func.count()).select_from(WaitlistRequest)) == 0
        assert await action('recover_waitlist')
        assert 'SMS' in telegram.sent[-1][1]
        assert await session.scalar(select(func.count()).select_from(WaitlistRequest)) == 0
        assert await action('recover_waitlist_decline')
        assert not await action('recover_waitlist_confirm')
        assert await action('recover_waitlist')
        assert await action('recover_waitlist_confirm')
        request = (await session.execute(select(WaitlistRequest).options(selectinload(WaitlistRequest.services)))).scalar_one()
        assert request.preferred_master_id == master.id
        assert request.desired_date == day and request.notification_consent
        assert [item.id for item in request.services] == [original_service.id]
        assert await action('recover_waitlist_confirm')
        assert 'уже активний' in telegram.sent[-1][1]
        assert await session.scalar(select(func.count()).select_from(WaitlistRequest)) == 1
        assert secret not in str(bot.payload_json) + str(telegram.sent)
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize('phone,chat', [(None,'123'), ('+380501112233','other-chat')])
async def test_waitlist_cannot_use_missing_or_other_chat_contact(monkeypatch, phone, chat):
    bot = draft(future_day())
    bot.payload_json['booking_recovery']['consent_requested'] = True
    session = FakeDateTimeSession(bot)
    contact = SimpleNamespace(chat_id=chat, phone=phone, first_name='Client', last_name=None)
    async def forbidden(*args, **kwargs):
        pytest.fail('Waitlist created from an unowned or missing contact')
    monkeypatch.setattr(routes.telegram_waitlist_service, 'create', forbidden)
    assert not await routes._handle_booking_recovery_callback(FakeTelegramMessageProvider(), session,
        chat_id='123', text='recover_waitlist_confirm:'+'a'*16,
        telegram_contact=contact, callback_query_id='cb')


@pytest.mark.anyio
async def test_confirmation_conflict_refreshes_live_slots_on_correct_date(database):
    telegram = FakeTelegramMessageProvider()
    async with database() as session:
        day, _, customer, master, _, service, _, contact, bot = await seed(session, windows=False)
        start = local_time(day)
        session.add(MasterAvailabilityWindow(master_id=master.id, start_at=start, end_at=local_time(day, 12)))
        session.add(Booking(master_id=master.id, service_id=service.id, customer_id=customer.id,
            customer_name='Client', customer_phone=customer.phone, start_at=start,
            end_at=start+timedelta(minutes=30), source=BookingSource.telegram))
        bot.state = routes.TELEGRAM_READY_TO_BOOK_STATE
        bot.payload_json = {**bot.payload_json, 'selected_visit_time': start.isoformat(),
                            'confirmation_token': 'c'*32, 'slot_token': 'old-slot'}
        await session.commit()
        assert not await routes._handle_booking_confirmation(telegram, session, chat_id='123',
            telegram_contact=contact, background_tasks=None, confirmation_token='c'*32)
        assert bot.state == routes.TELEGRAM_SELECTING_TIME_STATE
        assert bot.payload_json['selected_visit_date'] == day.isoformat()
        assert 'selected_visit_time' not in bot.payload_json and 'confirmation_token' not in bot.payload_json
        assert bot.payload_json['slot_token'] != 'old-slot'
        buttons = callbacks(telegram.sent[-1][2])
        assert any('T10:30:00|' in button for button in buttons)
        assert not any('T10:00:00|' in button for button in buttons)
        assert await session.scalar(select(func.count()).select_from(Booking)) == 1
