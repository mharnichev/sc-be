"""Failed Telegram actions retry; successful events dedup independently of order."""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from test_inventory_integration import anyio_backend, database  # noqa: F401
from app.api.v1.routes import messaging as routes
from app.core.config import settings
from app.models.booking import Master
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, ConsentStatus, TelegramBotSession, TelegramContact, TelegramWebhookEvent
from app.services.messaging import ProviderSendResult
from app.services.telegram_inbox import process_telegram_event

pytestmark = pytest.mark.anyio


class Request:
    def __init__(self, update):
        self.update = update
    async def json(self):
        return self.update


class Provider:
    def __init__(self):
        self.calls = []
    async def send_message(self, **kwargs):
        self.calls.append(kwargs["destination"])
        return ProviderSendResult("synthetic-inbox", {"ok": True})


@pytest.mark.parametrize("configured, supplied, expected", [(None, None, 503), ("synthetic-secret", "wrong", 401), ("synthetic-secret", "synthetic-secret", None)])
async def test_production_webhook_secret_is_checked_before_parse_or_business_action(monkeypatch, configured, supplied, expected):
    from app.services import telegram_inbox
    calls = []
    class TrackedRequest:
        async def json(self):
            calls.append("parse")
            return {"update_id": 100, "message": {"chat": {"id": 1, "type": "private"}}}
    async def process(session, **kwargs):
        calls.append("process")
        return {"ok": True, "handled": True}
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    monkeypatch.setattr(settings, "telegram_webhook_secret", configured)
    monkeypatch.setattr(telegram_inbox, "process_telegram_event", process)
    if expected:
        with pytest.raises(HTTPException) as error:
            await routes.telegram_webhook(TrackedRequest(), supplied, None, object())
        assert error.value.status_code == expected
        assert calls == []
    else:
        assert await routes.telegram_webhook(TrackedRequest(), supplied, None, object()) == {"ok": True, "handled": True}
        assert calls == ["parse", "process"]


def update(number, master):
    return {"update_id": number, "message": {"chat": {"id": 987654321, "type": "private"},
        "from": {"id": 987654321, "first_name": "Synthetic"},
        "text": "/start " + routes._master_connect_token(master)}}


async def call(database, payload):
    async with database() as session:
        return await routes.telegram_webhook(Request(payload), x_telegram_bot_api_secret_token=None,
            background_tasks=None, session=session)


@pytest.fixture
def provider(monkeypatch):
    provider = Provider()
    monkeypatch.setattr(routes, "TelegramMessageProvider", lambda: provider)
    monkeypatch.setattr(settings, "telegram_webhook_secret", None)
    return provider


async def test_failed_business_action_retries_same_update_after_recovery_and_success_dedups(database, provider):
    payload = update(100, 7001)
    with pytest.raises(HTTPException) as caught:
        await call(database, payload)
    assert caught.value.status_code == 404
    async with database() as session:
        row = await session.get(TelegramWebhookEvent, 100)
        assert row.state == "failed" and row.attempts == 1
        session.add(Master(id=7001, full_name="Synthetic recovered master", is_active=True))
        await session.commit()
    assert (await call(database, payload))["master_id"] == 7001
    assert (await call(database, payload))["action"] == "duplicate_update"
    async with database() as session:
        assert (await session.get(Master, 7001)).telegram_chat_id == "987654321"
        event = await session.get(TelegramWebhookEvent, 100)
        assert event.state == "completed" and event.attempts == 2
    assert provider.calls == ["987654321"]


async def test_older_unhandled_valid_action_is_assessed_and_processed(database, provider):
    async with database() as session:
        session.add_all([Master(id=7001, full_name="Synthetic older", is_active=True),
            Master(id=7002, full_name="Synthetic newer", is_active=True)])
        await session.commit()
    assert (await call(database, update(201, 7002)))["master_id"] == 7002
    assert (await call(database, update(200, 7001)))["master_id"] == 7001
    async with database() as session:
        assert (await session.get(Master, 7001)).telegram_chat_id == "987654321"
        assert (await session.get(Master, 7002)).telegram_chat_id == "987654321"
    assert len(provider.calls) == 2


async def test_older_connect_for_same_resource_is_rejected_as_stale_without_replaying_reply(database, provider):
    async with database() as session:
        session.add(Master(id=7001, full_name="Synthetic current connection", is_active=True))
        await session.commit()
    newer = update(201, 7001)
    newer["message"]["chat"]["id"] = 222
    older = update(200, 7001)
    older["message"]["chat"]["id"] = 111
    assert (await call(database, newer))["master_id"] == 7001
    assert (await call(database, older))["action"] == "stale_connect"
    async with database() as session:
        assert (await session.get(Master, 7001)).telegram_chat_id == "222"
    assert provider.calls == ["222"]


@pytest.mark.parametrize("older_action", ["select_master:7001", "new_booking", "Скасувати"])
async def test_older_draft_action_cannot_overwrite_a_newer_master_selection(database, provider, older_action):
    async with database() as session:
        session.add_all([Master(id=7001, full_name="Synthetic older", is_active=True),
            Master(id=7002, full_name="Synthetic newer", is_active=True),
            TelegramContact(chat_id="987654321", phone="+380501112292")])
        await session.commit()
    def draft_update(number, action):
        return {"update_id": number, "message": {"chat": {"id": 987654321, "type": "private"},
            "from": {"id": 987654321}, "text": action}}
    assert (await call(database, draft_update(201, "select_master:7002")))["action"] == "select_master"
    calls_before = len(provider.calls)
    async with database() as session:
        bot = await session.scalar(select(TelegramBotSession).where(TelegramBotSession.chat_id == "987654321"))
        assert bot.state == routes.TELEGRAM_SELECTING_SERVICES_STATE
        payload_before = dict(bot.payload_json)
    assert (await call(database, draft_update(200, older_action)))["action"] == "stale_draft_action"
    async with database() as session:
        bot = await session.scalar(select(TelegramBotSession).where(TelegramBotSession.chat_id == "987654321"))
        assert bot.selected_master_id == 7002 and bot.state == routes.TELEGRAM_SELECTING_SERVICES_STATE
        assert bot.payload_json == payload_before
    assert len(provider.calls) == calls_before
    # An unrelated older resource action still receives its own validity check.
    assert (await call(database, update(199, 7001)))["master_id"] == 7001


async def test_successful_business_action_is_deduplicated_even_when_reply_fails(database, provider):
    async def failed_reply(**kwargs):
        provider.calls.append(kwargs["destination"])
        raise TimeoutError("synthetic reply timeout")
    provider.send_message = failed_reply
    async with database() as session:
        session.add(Master(id=7001, full_name="Synthetic linked despite reply failure", is_active=True))
        await session.commit()
    payload = update(100, 7001)
    assert (await call(database, payload))["master_id"] == 7001
    assert (await call(database, payload))["action"] == "duplicate_update"
    assert len(provider.calls) == 1


async def test_chat_lock_spans_action_commit_and_releases_after_action(database):
    entered, release = asyncio.Event(), asyncio.Event()
    pids = []
    calls = 0
    async def action(session):
        nonlocal calls
        calls += 1
        pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        await session.commit()
        entered.set()
        await release.wait()
        pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        return {"ok": True, "handled": True, "action": "synthetic_success"}
    async def worker():
        async with database() as session:
            return await process_telegram_event(session, update_id=100, chat_id="synthetic-chat", action=action)
    first = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(HTTPException) as busy:
            await worker()
        assert busy.value.status_code == 503 and calls == 1
        release.set()
        await asyncio.wait_for(first, 5)
        assert pids[0] == pids[1]
        assert (await worker())["action"] == "duplicate_update"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("restricted", ["do_not_contact", "opted_out", "unknown"])
async def test_admin_manual_messages_obey_consent_without_override(database, provider, restricted):
    async with database() as session:
        customer = Customer(name="Synthetic manual recipient", phone="+380501112296")
        session.add(customer)
        await session.flush()
        preference = ClientCommunicationPreference(customer_id=customer.id, telegram_chat_id="synthetic-chat",
            marketing_consent=ConsentStatus.opted_in, do_not_contact=restricted == "do_not_contact")
        if restricted != "do_not_contact":
            preference.marketing_consent = ConsentStatus(restricted)
        session.add(preference)
        await session.commit()
        with pytest.raises(HTTPException) as denied:
            await routes.send_customer_manual_message(customer.id,
                routes.ManualCustomerMessageRequest(body="Synthetic marketing"), object(), session)
        assert denied.value.status_code == 400
    assert provider.calls == []


async def test_inactive_flag_is_informational_and_manual_delivery_key_deduplicates(database, provider):
    async with database() as session:
        customer = Customer(name="Synthetic inactive recipient", phone="+380501112295", is_active=False)
        session.add(customer)
        await session.flush()
        session.add(ClientCommunicationPreference(customer_id=customer.id, telegram_chat_id="synthetic-chat",
            marketing_consent=ConsentStatus.opted_in))
        await session.commit()
        payload = routes.ManualCustomerMessageRequest(body="Synthetic marketing", idempotency_key="synthetic-manual")
        first = await routes.send_customer_manual_message(customer.id, payload, object(), session)
        second = await routes.send_customer_manual_message(customer.id, payload, object(), session)
        assert first["sent"] is True and second["sent"] is True
        assert second["provider_response"]["deduplicated"] is True
    assert provider.calls == ["synthetic-chat"]


async def test_business_commit_then_cancelled_reply_deduplicates_effect_on_restart(database, provider):
    async def cancelled_reply(**kwargs):
        provider.calls.append(kwargs["destination"])
        raise asyncio.CancelledError("synthetic interrupted reply")
    provider.send_message = cancelled_reply
    async with database() as session:
        session.add(Master(id=7001, full_name="Synthetic linked before cancellation", is_active=True))
        await session.commit()
    payload = update(100, 7001)
    with pytest.raises(asyncio.CancelledError):
        await call(database, payload)
    async with database() as session:
        assert (await session.get(TelegramWebhookEvent, 100)).state == "completed"
        assert (await session.get(Master, 7001)).telegram_chat_id == "987654321"
    assert (await call(database, payload))["action"] == "duplicate_update"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("definitive", [True, False])
async def test_photo_fallback_requires_definitive_rejection_and_same_event_never_repeats_uncertain_photo(database, definitive):
    from app.services.telegram_delivery import TelegramRejected
    class PhotoProvider(Provider):
        photo_calls = 0
        async def send_photo(self, **kwargs):
            self.photo_calls += 1
            if definitive:
                raise TelegramRejected("synthetic definitive photo rejection")
            raise TimeoutError("synthetic uncertain photo outcome")
    provider = PhotoProvider()
    async def action(session):
        photo_sent_or_uncertain = await routes._safe_send_telegram_photo(provider,
            destination="synthetic-chat", photo_url="https://example.test/synthetic-photo.jpg", caption="Synthetic")
        if not photo_sent_or_uncertain:
            await routes._safe_send_telegram_message(provider, destination="synthetic-chat", body="Synthetic fallback")
        return {"ok": True, "handled": True, "action": "synthetic_success"}
    async with database() as session:
        await process_telegram_event(session, update_id=100, chat_id="synthetic-chat", action=action)
        assert (await process_telegram_event(session, update_id=100, chat_id="synthetic-chat", action=action))["action"] == "duplicate_update"
    assert provider.photo_calls == 1
    assert len(provider.calls) == int(definitive)


async def test_single_connection_pool_can_commit_business_action_and_reply_without_starvation(database):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.services.telegram_inbox import commit_event_business_action, send_event_reply
    async with database() as session:
        schema = await session.scalar(text("SELECT current_schema()"))
    engine = create_async_engine(database.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=0.25,
        connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = Provider()
    async def action(session):
        await commit_event_business_action(session)
        await send_event_reply(provider, operation="message", destination="synthetic-chat",
            payload={"body": "Synthetic", "reply_markup": None})
        return {"ok": True, "handled": True, "action": "synthetic_success"}
    try:
        async with sessions() as session:
            assert (await asyncio.wait_for(process_telegram_event(session,
                update_id=100, chat_id="synthetic-chat", action=action), 5))["handled"] is True
            assert (await process_telegram_event(session,
                update_id=100, chat_id="synthetic-chat", action=action))["action"] == "duplicate_update"
        assert len(provider.calls) == 1
    finally:
        await engine.dispose()


async def test_failed_event_retry_with_changed_reply_body_never_repeats_uncertain_slot(database):
    provider = Provider()
    bodies = []
    async def uncertain_reply(**kwargs):
        bodies.append(kwargs["body"])
        raise TimeoutError("synthetic uncertain reply")
    provider.send_message = uncertain_reply
    reply_body = "Synthetic initial failure feedback"
    async def action(session):
        await routes._safe_send_telegram_message(provider, destination="synthetic-chat", body=reply_body)
        return {"ok": True, "handled": True, "action": "synthetic_failed"}
    async with database() as session:
        await process_telegram_event(session, update_id=100, chat_id="synthetic-chat", action=action)
        reply_body = "Synthetic changed feedback after recovery"
        await process_telegram_event(session, update_id=100, chat_id="synthetic-chat", action=action)
    assert bodies == ["Synthetic initial failure feedback"]
