"""Telegram self-service accepts only explicit, valid private chat contexts."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.v1.routes import messaging as routes
from app.models.booking import Booking, BookingStatus
from app.models.messaging import ClientCommunicationPreference, TelegramBotSession, TelegramContact, TelegramWebhookEvent
from test_inventory_integration import database, anyio_backend  # noqa: F401
from test_telegram_booking_flow import seed
from test_telegram_connect import FakeTelegramMessageProvider
from test_telegram_inbox_recovery_integration import Request


def update(chat, action, shape, number=1):
    message = {'chat': chat, 'from': {'id': 666}, 'text': action}
    if action == 'contact':
        message.pop('text')
        message['contact'] = {'user_id': 666, 'phone_number': '+380501112233'}
    if shape == 'callback_query':
        return {'update_id': number, 'callback_query': {'id': str(number),
            'from': {'id': 666}, 'data': action, 'message': message}}
    return {'update_id': number, shape: message}


@pytest.mark.anyio
@pytest.mark.parametrize('shape', ['message', 'edited_message', 'callback_query'])
@pytest.mark.parametrize('chat', [
    {'id': -100123, 'type': 'group'}, {'id': -100123, 'type': 'supergroup'},
    {'id': -100123, 'type': 'channel'}, {'id': 123, 'type': 'group'},
    {'id': -100123, 'type': 'private'}, {'id': 0, 'type': 'private'},
    {'id': True, 'type': 'private'}, {'id': '123', 'type': 'private'},
    {'id': 123}, {'type': 'private'}, None,
])
@pytest.mark.parametrize('action', ['/booking', 'reschedule_booking:99',
    'manage_cancel:nonce', 'confirm_booking:nonce', '/start customer-token', 'contact'])
async def test_non_private_or_malformed_chat_cannot_reach_business_or_contact_mutation(monkeypatch, shape, chat, action):
    async def forbidden(*args, **kwargs):
        pytest.fail('Rejected chat reached inbox projection or business processing')
    monkeypatch.setattr(routes, '_telegram_update_already_processed', forbidden)
    monkeypatch.setattr(routes, '_upsert_telegram_contact_from_update', forbidden)
    result = await routes._process_telegram_update(update(chat, action, shape), object(), None)
    assert result == {'ok': True, 'handled': True, 'action': 'non_private_chat_ignored'}


@pytest.mark.anyio
@pytest.mark.parametrize('shape', ['channel_post', 'edited_channel_post'])
async def test_real_channel_post_shapes_are_ignored_before_business_processing(monkeypatch, shape):
    async def forbidden(*args, **kwargs):
        pytest.fail('Channel post reached business processing')
    monkeypatch.setattr(routes, '_telegram_update_already_processed', forbidden)
    result = await routes._process_telegram_update(
        update({'id': -100123, 'type': 'channel'}, '/booking', shape), object(), None)
    assert result['action'] == 'non_private_chat_ignored'


@pytest.mark.anyio
async def test_legacy_group_contact_cannot_book_manage_link_or_overwrite_identity_and_inbox_is_retained(database, monkeypatch):
    monkeypatch.setattr(routes.settings, 'telegram_webhook_secret', 'private-gate-test')
    monkeypatch.setattr(routes, 'TelegramMessageProvider', FakeTelegramMessageProvider)
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []
    async with database() as session:
        customer, master, _, services, contact = await seed(session)
        contact.chat_id = '-100123'
        contact.telegram_user_id = '123'
        contact.first_name = 'Owner'
        contact.raw_update = {'original': True}
        bot = TelegramBotSession(chat_id=contact.chat_id, telegram_contact_id=contact.id,
            linked_customer_id=customer.id, state='ready_to_book',
            selected_master_id=master.id, selected_service_id=services[0].id,
            payload_json={'confirmation_token': 'nonce', 'booking_management': {'token': 'nonce'}})
        start = datetime.now(UTC) + timedelta(days=2)
        booking = Booking(master_id=master.id, service_id=services[0].id,
            customer_id=customer.id, customer_name=customer.name, customer_phone=customer.phone,
            start_at=start, end_at=start + timedelta(minutes=30), status=BookingStatus.confirmed)
        preference = ClientCommunicationPreference(customer_id=customer.id, telegram_chat_id='private-owner')
        session.add_all([bot, booking, preference])
        await session.commit()
        identifiers = customer.id, master.id, contact.id, bot.id, booking.id
        before = deepcopy(bot.payload_json)
        actions = ['/booking', f'reschedule_booking:{booking.id}', 'manage_cancel:nonce',
                   'confirm_booking:nonce', '/start ' + routes._customer_connect_token(customer.id),
                   '/start ' + routes._master_connect_token(master.id), 'contact']

    number = 100
    for shape in ('message', 'callback_query'):
        for action in actions:
            number += 1
            payload = update({'id': -100123, 'type': 'supergroup'}, action, shape, number)
            async with database() as session:
                result = await routes.telegram_webhook(Request(payload), 'private-gate-test', None, session)
                assert result['action'] == 'non_private_chat_ignored'
            async with database() as session:
                event = await session.get(TelegramWebhookEvent, number)
                assert event.state == 'completed' and event.result_json['action'] == 'non_private_chat_ignored'

    async with database() as session:
        replay = await routes.telegram_webhook(Request(payload), 'private-gate-test', None, session)
        assert replay['action'] == 'duplicate_update'

    async with database() as session:
        customer_id, master_id, contact_id, bot_id, booking_id = identifiers
        contact = await session.get(TelegramContact, contact_id)
        assert contact.telegram_user_id == '123' and contact.first_name == 'Owner'
        assert contact.linked_customer_id == customer_id and contact.raw_update == {'original': True}
        bot = await session.get(TelegramBotSession, bot_id)
        assert bot.payload_json == before and bot.state == 'ready_to_book'
        assert (await session.get(Booking, booking_id)).status == BookingStatus.confirmed
        assert (await session.get(routes.Master, master_id)).telegram_chat_id is None
        preference = await session.scalar(select(ClientCommunicationPreference))
        assert preference.telegram_chat_id == 'private-owner'
        assert len(list(await session.scalars(select(Booking.id)))) == 1
    assert FakeTelegramMessageProvider.sent == FakeTelegramMessageProvider.answered_callbacks == []
