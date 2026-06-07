from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.routes import messaging as messaging_routes
from app.core.config import settings
from app.models.messaging import ConsentStatus
from app.services.messaging import ProviderSendResult


class FakeSession:
    async def get(self, model, entity_id):  # noqa: ANN001
        return SimpleNamespace(id=entity_id)


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self) -> dict:
        return self.payload


class FakeMessagingService:
    def __init__(self) -> None:
        self.upserts: list[tuple[int, dict]] = []

    async def upsert_preference(self, session, customer_id: int, data: dict):  # noqa: ANN001
        self.upserts.append((customer_id, data))
        return SimpleNamespace(customer_id=customer_id, telegram_chat_id=data["telegram_chat_id"])


class FakeTelegramMessageProvider:
    sent: list[tuple[str, str]] = []
    answered_callbacks: list[str] = []

    async def send_message(self, *, destination: str, body: str) -> ProviderSendResult:
        self.sent.append((destination, body))
        return ProviderSendResult(provider_message_id="1", raw_response={"ok": True})

    async def answer_callback_query(self, *, callback_query_id: str, text: str | None = None) -> dict:
        self.answered_callbacks.append(callback_query_id)
        return {"ok": True}


@pytest.mark.anyio
async def test_customer_telegram_connect_link_contains_signed_start_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_username", "SoulcutsBot")

    response = await messaging_routes.get_customer_telegram_connect_link(123, object(), FakeSession())
    token = str(response["connect_link"]).split("start=", maxsplit=1)[1]

    assert response["connect_link"].startswith("https://t.me/SoulcutsBot?start=")
    assert len(token) <= 64
    assert messaging_routes._customer_id_from_connect_token(token) == 123


@pytest.mark.anyio
async def test_telegram_webhook_saves_customer_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service = FakeMessagingService()
    FakeTelegramMessageProvider.sent = []
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "service", fake_service)
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    token = messaging_routes._customer_connect_token(123)
    request = FakeRequest(
        {
            "message": {
                "text": f"/start {token}",
                "chat": {"id": 987654321},
            }
        }
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=FakeSession(),
    )

    assert response == {"ok": True, "handled": True, "customer_id": 123, "telegram_chat_id": "987654321"}
    assert fake_service.upserts == [
        (
            123,
            {
                "telegram_chat_id": "987654321",
                "transactional_consent": ConsentStatus.opted_in,
            },
        )
    ]
    assert FakeTelegramMessageProvider.sent == [
        ("987654321", "Telegram підключено. Тепер ми зможемо надсилати вам повідомлення про записи.")
    ]


@pytest.mark.anyio
async def test_telegram_webhook_replies_with_booking_link(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "public_site_url", "https://soulcuts.com.ua")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    request = FakeRequest(
        {
            "message": {
                "text": "Новий запис",
                "chat": {"id": 987654321},
            }
        }
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=FakeSession(),
    )

    assert response == {"ok": True, "handled": True, "action": "new_booking_link"}
    assert FakeTelegramMessageProvider.sent == [
        ("987654321", "Для нового запису відкрийте онлайн-форму: https://soulcuts.com.ua/#booking")
    ]


@pytest.mark.anyio
async def test_telegram_webhook_replies_with_booking_link_for_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "public_site_url", "https://soulcuts.com.ua")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    request = FakeRequest(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "new_booking",
                "message": {
                    "chat": {"id": 987654321},
                },
            }
        }
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=FakeSession(),
    )

    assert response == {"ok": True, "handled": True, "action": "new_booking_link"}
    assert FakeTelegramMessageProvider.answered_callbacks == ["callback-1"]
    assert FakeTelegramMessageProvider.sent == [
        ("987654321", "Для нового запису відкрийте онлайн-форму: https://soulcuts.com.ua/#booking")
    ]


@pytest.mark.anyio
async def test_telegram_webhook_replies_to_unsupported_legacy_command(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "public_site_url", "https://soulcuts.com.ua")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    request = FakeRequest(
        {
            "message": {
                "text": "Прайс",
                "chat": {"id": 987654321},
            }
        }
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=FakeSession(),
    )

    assert response == {"ok": True, "handled": True, "action": "unsupported_command_fallback"}
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Ця команда поки недоступна в Telegram. "
            "Для запису скористайтесь онлайн-формою: https://soulcuts.com.ua/#booking",
        )
    ]
