from datetime import datetime
from types import SimpleNamespace

import pytest

from app.api.v1.routes import messaging as routes
from app.models.messaging import TelegramContact
from test_telegram_connect import (
    FakeScalarResult, FakeTelegramMessageProvider, FakeMasterSelectionSession,
    FakeServiceSelectionSession, FakeDateTimeSession,
)

TOKEN = 'a' * 32

@pytest.fixture(autouse=True)
def isolated_replies(monkeypatch):
    from app.services import telegram_inbox
    async def reply(*args, **kwargs):
        return None
    monkeypatch.setattr(telegram_inbox, 'send_event_reply', reply)
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []


def draft(state=None):
    return SimpleNamespace(
        selected_master_id=1, selected_service_id=10,
        state=state or routes.TELEGRAM_READY_TO_BOOK_STATE,
        linked_customer_id=77, telegram_contact_id=1,
        payload_json={'selected_service_ids': [10], 'selected_visit_date': '2026-11-12',
                      'selected_visit_time': '2026-11-12T10:00:00+02:00',
                      'booking_id': 99, 'confirmation_token': TOKEN, 'slot_token': 'b'*16},
    )


@pytest.mark.anyio
@pytest.mark.parametrize('sender,owner', [(555,555), (555,666), (555,None), (None,555),
                                        ('555',555), (555,'555'), (True,1), (555,True),
                                        ({'id':555},555), (555,[]), (0,0), (-1,-1)])
async def test_contact_ownership_prevents_linking_unverified_phone(monkeypatch, sender, owner):
    existing = TelegramContact(chat_id='123', phone='old phone', linked_customer_id=88)
    class Session:
        async def execute(self, statement):
            assert 'telegram_contacts' in str(statement)
            return FakeScalarResult(existing)
    looked_up = []
    linked = []
    async def lookup(session, phone):
        looked_up.append(phone)
        return 77
    async def link(session, **kwargs):
        linked.append(kwargs)
    monkeypatch.setattr(routes, '_customer_id_by_phone', lookup)
    monkeypatch.setattr(routes, '_link_customer_telegram_preference', link)
    update = {'message': {'chat': {'id':123, 'type': 'private'}, 'from': {'id':sender},
                          'contact': {'user_id':owner, 'phone_number':'new phone'}}}
    await routes._upsert_telegram_contact_from_update(Session(), update)
    valid = type(sender) is int and sender > 0 and type(owner) is int and owner == sender
    if valid:
        assert existing.phone == 'new phone'
        assert existing.linked_customer_id == 77
        assert looked_up == ['new phone'] and len(linked) == 1
    else:
        assert existing.phone == 'old phone'
        assert existing.linked_customer_id == 88
        assert not looked_up and not linked


@pytest.mark.anyio
@pytest.mark.parametrize('new_master', [1,2])
async def test_master_selection_invalidates_all_dependent_choices(new_master):
    bot = draft()
    master = SimpleNamespace(id=new_master, full_name='Master')
    session = FakeMasterSelectionSession(master)
    session.bot_session = bot
    assert await routes._handle_master_selection(FakeTelegramMessageProvider(), session,
        chat_id='123', master_id=new_master, telegram_contact=None, callback_query_id='cb')
    assert bot.selected_master_id == new_master
    assert bot.selected_service_id is None
    assert not bot.payload_json.get('selected_service_ids')
    for key in ('selected_visit_date','selected_visit_time','booking_id','confirmation_token','slot_token'):
        assert not bot.payload_json.get(key)


@pytest.mark.anyio
@pytest.mark.parametrize('initial,clicked,expected', [([10],20,[10,20]), ([10,20],10,[20]), ([10],10,[])])
async def test_service_add_remove_invalidates_date_time_confirmation(initial,clicked,expected):
    bot = draft()
    bot.payload_json['selected_service_ids'] = initial
    session = FakeServiceSelectionSession(bot, SimpleNamespace(id=clicked))
    session.services.extend(
        SimpleNamespace(id=service_id, name='Service', price=1200, duration_minutes=30)
        for service_id in initial if service_id != clicked
    )
    assert await routes._handle_service_selection(FakeTelegramMessageProvider(), session,
        chat_id='123', service_id=clicked, callback_query_id='cb')
    assert bot.payload_json['selected_service_ids'] == expected
    assert bot.selected_service_id == (expected[0] if expected else None)
    for key in ('selected_visit_date','selected_visit_time','booking_id','confirmation_token','slot_token'):
        assert not bot.payload_json.get(key)


@pytest.mark.anyio
@pytest.mark.parametrize('state,token', [(routes.TELEGRAM_READY_TO_BOOK_STATE,None),
    (routes.TELEGRAM_READY_TO_BOOK_STATE,'c'*32), (routes.TELEGRAM_MASTER_SELECTED_STATE,TOKEN),
    (routes.TELEGRAM_SELECTING_SERVICES_STATE,TOKEN), (routes.TELEGRAM_BOOKED_STATE,'c'*32),
    (routes.TELEGRAM_READY_TO_BOOK_STATE,''), (routes.TELEGRAM_READY_TO_BOOK_STATE,[]),
    (routes.TELEGRAM_READY_TO_BOOK_STATE,123)])
async def test_old_or_missing_confirmation_cannot_create_booking(monkeypatch,state,token):
    bot = draft(state)
    session = FakeDateTimeSession(bot)
    async def forbidden(*args, **kwargs):
        pytest.fail('Invalid confirmation reached booking creation')
    monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',forbidden)
    assert not await routes._handle_booking_confirmation(FakeTelegramMessageProvider(),session,
        chat_id='123',telegram_contact=None,background_tasks=None,confirmation_token=token)
    assert session.commit_count == 1


@pytest.mark.anyio
async def test_matching_confirmation_retry_does_not_duplicate_booking(monkeypatch):
    bot = draft(routes.TELEGRAM_BOOKED_STATE)
    async def forbidden(*args, **kwargs):
        pytest.fail('Retry duplicated booking')
    monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',forbidden)
    assert await routes._handle_booking_confirmation(FakeTelegramMessageProvider(),FakeDateTimeSession(bot),
        chat_id='123',telegram_contact=None,background_tasks=None,confirmation_token=TOKEN)


@pytest.mark.parametrize('prices,total,expected_lines,expected_total', [
    ({10:500,20:300},650,['(500 грн)','(300 грн)'],'650'),
    ({10:0,20:0},0,['(0 грн)','(0 грн)'],'0'),
    ({10:500,20:300},None,['(500 грн)','(300 грн)'],'800'),
    ({10:500},500,['(500 грн)','(2000 грн)'],'500'),
    ({},None,['(1000 грн)','(2000 грн)'],'3000'),
])
def test_booking_card_uses_saved_prices_and_discounted_total(prices,total,expected_lines,expected_total):
    booking = SimpleNamespace(master=SimpleNamespace(full_name='Master'), service_prices=prices,
        total_amount=total, services=[SimpleNamespace(id=10,name='Hair',price=1000),
        SimpleNamespace(id=20,name='Beard',price=2000)],
        start_at=datetime(2026,11,12,10,tzinfo=routes.KYIV_TZ),
        end_at=datetime(2026,11,12,11,tzinfo=routes.KYIV_TZ), customer_comment='')
    body = routes._booking_view_message(booking)
    for line in expected_lines:
        assert line in body
    assert body.endswith(f'Загальна вартість: {expected_total} грн')


@pytest.mark.anyio
@pytest.mark.parametrize('slot_token,state,visit_date', [(None,routes.TELEGRAM_SELECTING_TIME_STATE,'2026-11-12'),
    ('c'*16,routes.TELEGRAM_SELECTING_TIME_STATE,'2026-11-12'),
    ('b'*16,routes.TELEGRAM_SELECTING_TIME_STATE,'2026-11-13'),
    ('b'*16,routes.TELEGRAM_READY_TO_BOOK_STATE,'2026-11-12')])
async def test_stale_time_callback_cannot_overwrite_current_draft(slot_token,state,visit_date):
    bot = draft(state)
    bot.payload_json['selected_visit_date'] = visit_date
    prior = dict(bot.payload_json)
    session = FakeDateTimeSession(bot)
    assert not await routes._handle_time_selection(FakeTelegramMessageProvider(),session,
        chat_id='123', visit_time=datetime(2026,11,12,10,tzinfo=routes.KYIV_TZ),
        callback_query_id='cb',slot_token=slot_token)
    assert bot.payload_json == prior
    assert session.commit_count == 1


from test_inventory_integration import database, anyio_backend  # noqa: E402,F401
from app.models.customer import Customer  # noqa: E402
from sqlalchemy import select  # noqa: E402


@pytest.mark.anyio
@pytest.mark.parametrize('owner', [555,666,None])
async def test_real_database_contact_cannot_relink_existing_customer_to_foreign_phone(database,owner):
    async with database() as session:
        old = Customer(phone='+380501111111',name='Own customer')
        other = Customer(phone='+380502222222',name='Other customer')
        session.add_all([old,other])
        await session.flush()
        contact = TelegramContact(chat_id='123', telegram_user_id='555',
                                  phone=old.phone,linked_customer_id=old.id)
        session.add(contact)
        await session.commit()
        old_id, other_id = old.id, other.id
        update = {'update_id':1, 'message':{'chat':{'id':123, 'type': 'private'}, 'from':{'id':555},
            'contact':{'user_id':owner,'phone_number':other.phone}}}
        await routes._upsert_telegram_contact_from_update(session,update)
        await session.commit()
    async with database() as session:
        saved = (await session.execute(select(TelegramContact).where(TelegramContact.chat_id=='123'))).scalar_one()
        assert saved.linked_customer_id == (other_id if owner==555 else old_id)
        assert saved.phone == ('+380502222222' if owner==555 else '+380501111111')
        preferences = (await session.execute(select(routes.ClientCommunicationPreference))).scalars().all()
        assert len(preferences) == (1 if owner==555 else 0)


@pytest.mark.anyio
@pytest.mark.parametrize('visit_time,visit_date,services', [
    ('bad timestamp','2026-11-12',[10]),
    ('2026-11-12T10:00:00','2026-11-12',[10]),
    ('2026-11-12T10:00:00+02:00','2026-11-13',[10]),
    ('2026-11-12T10:00:00+02:00',None,[10]),
    ('2026-11-12T10:00:00+02:00','2026-11-12',[]),
    (None,'2026-11-12',[10]),
])
async def test_matching_token_cannot_book_incomplete_or_inconsistent_draft(monkeypatch,visit_time,visit_date,services):
    bot = draft()
    bot.payload_json.update(selected_visit_time=visit_time,selected_visit_date=visit_date,selected_service_ids=services)
    async def forbidden(*args, **kwargs):
        pytest.fail('Inconsistent draft reached booking creation')
    monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',forbidden)
    session = FakeDateTimeSession(bot)
    assert not await routes._handle_booking_confirmation(FakeTelegramMessageProvider(),session,
        chat_id='123',telegram_contact=None,background_tasks=None,confirmation_token=TOKEN)
    assert session.commit_count == 0


@pytest.mark.parametrize('callback', [None,'select_time:','select_time:not-a-date',
    'select_time:2026-02-30T10:00:00|'+ 'b'*16,'select_time:2026-11-12T99:00:00|'+ 'b'*16])
def test_malformed_time_callbacks_are_rejected(callback):
    assert routes._selected_visit_time_from_callback(callback) is None


@pytest.mark.parametrize('snapshot,total', [(500,400),(0,0),(None,None)])
def test_orm_booking_card_keeps_service_item_snapshot_after_catalog_change(snapshot,total):
    from app.models.booking import BarberService, Booking, BookingServiceItem, Master
    service = BarberService(id=10,name='Hair',price=1000)
    booking = Booking(
        master=Master(full_name='Master'), service_items=[
            BookingServiceItem(service_id=10,service=service,price_amount=snapshot,position=0)
        ], total_amount=total,
        start_at=datetime(2026,11,12,10,tzinfo=routes.KYIV_TZ),
        end_at=datetime(2026,11,12,11,tzinfo=routes.KYIV_TZ), customer_comment='',
    )
    service.price = 2500
    expected_price = 2500 if snapshot is None else snapshot
    expected_total = expected_price if total is None else total
    body = routes._booking_view_message(booking)
    assert f'({expected_price} грн)' in body
    assert body.endswith(f'Загальна вартість: {expected_total} грн')


@pytest.mark.parametrize('subtotal,total,expected', [(600,None,600),(0,0,0),(600,450,450),(None,None,2500)])
def test_orm_legacy_booking_card_uses_saved_subtotal(subtotal,total,expected):
    from app.models.booking import BarberService, Booking, Master
    service = BarberService(id=10,name='Hair',price=2500)
    booking = Booking(
        master=Master(full_name='Master'),service_id=10,service=service,service_items=[],
        subtotal_amount=subtotal,total_amount=total,
        start_at=datetime(2026,11,12,10,tzinfo=routes.KYIV_TZ),
        end_at=datetime(2026,11,12,11,tzinfo=routes.KYIV_TZ),customer_comment='',
    )
    body = routes._booking_view_message(booking)
    assert f'({2500 if subtotal is None else subtotal} грн)' in body
    assert body.endswith(f'Загальна вартість: {expected} грн')


@pytest.mark.anyio
@pytest.mark.parametrize('text', ['Забронювати','confirm_booking:','confirm_booking:old-token'])
async def test_legacy_or_forged_confirmation_dispatch_never_creates_booking(monkeypatch,text):
    bot = draft()
    session = FakeDateTimeSession(bot)
    async def contact(session,update):
        return SimpleNamespace(phone='0501112233')
    async def forbidden(*args,**kwargs):
        pytest.fail('Old confirmation created booking')
    monkeypatch.setattr(routes,'TelegramMessageProvider',FakeTelegramMessageProvider)
    monkeypatch.setattr(routes,'_upsert_telegram_contact_from_update',contact)
    monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',forbidden)
    result = await routes._process_telegram_update(
        {'callback_query': {'id':'old-confirm','data':text,'message':{'chat':{'id':123, 'type': 'private'}}}},
        session,None,
    )
    assert result['action'] == 'book_failed'
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.answered_callbacks == ['old-confirm']


@pytest.mark.anyio
@pytest.mark.parametrize('owner', [666,None])
async def test_foreign_contact_dispatch_rejects_before_upsert(monkeypatch,owner):
    async def forbidden(*args,**kwargs):
        pytest.fail('Foreign contact reached account mutation')
    monkeypatch.setattr(routes,'TelegramMessageProvider',FakeTelegramMessageProvider)
    monkeypatch.setattr(routes,'_upsert_telegram_contact_from_update',forbidden)
    result = await routes._process_telegram_update(
        {'message':{'chat':{'id':123, 'type': 'private'},'from':{'id':555},
                    'contact':{'user_id':owner,'phone_number':'0502222233'}}},
        FakeDateTimeSession(draft()),None,
    )
    assert result['action'] == 'contact_rejected'
    assert 'власним контактом' in FakeTelegramMessageProvider.sent[0][1]


@pytest.mark.anyio
@pytest.mark.parametrize('state', ['booked', 'booking_cancelled'])
@pytest.mark.parametrize('action', ['master_selection', 'services_action', 'service_selection', 'date_time_action', 'date_selection'])
async def test_terminal_draft_buttons_cannot_reopen_completed_attempt(monkeypatch, state, action):
    from datetime import date
    bot_session = draft(state)
    bot_session.payload_json['telegram_funnel_attempt_id'] = 'completed-attempt'
    before = dict(bot_session.payload_json)
    async def get_session(*args):
        return bot_session
    monkeypatch.setattr(routes, '_get_telegram_bot_session', get_session)
    class NoBusinessQueries:
        async def execute(self, statement):
            pytest.fail('Old terminal buttons must not query or change booking data')
    kwargs = {'chat_id': '123'}
    if action == 'master_selection':
        kwargs.update(master_id=1, telegram_contact=None, callback_query_id=None)
    elif action == 'service_selection':
        kwargs.update(service_id=10, callback_query_id=None)
    elif action == 'date_selection':
        kwargs.update(visit_date=date(2026,11,12), callback_query_id=None)
    result = await getattr(routes, '_handle_' + action)(FakeTelegramMessageProvider(), NoBusinessQueries(), **kwargs)
    assert result is False
    assert bot_session.state == state
    assert bot_session.payload_json == before
    assert 'Новий запис' in FakeTelegramMessageProvider.sent[-1][1]
    assert any(button["text"] == "Новий запис" for row in FakeTelegramMessageProvider.sent[-1][2]["keyboard"] for button in row)
