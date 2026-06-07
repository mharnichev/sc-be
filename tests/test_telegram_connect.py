from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.routes import messaging as messaging_routes
from app.core.config import settings
from app.models.messaging import ClientCommunicationPreference, ConsentStatus, TelegramContact
from app.services.messaging import ProviderSendResult


class FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def get(self, model, entity_id):  # noqa: ANN001
        return SimpleNamespace(id=entity_id)

    async def commit(self) -> None:
        self.committed = True


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


class FakeScalarResult:
    def __init__(self, value) -> None:  # noqa: ANN001
        self.value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.value


class FakeContactSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.customer_lookup_params: dict[str, object] = {}

    def add(self, value: object) -> None:
        self.added.append(value)

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_contacts" in sql:
            return FakeScalarResult(None)
        if "customers" in sql:
            self.customer_lookup_params = statement.compile().params
            return FakeScalarResult(SimpleNamespace(id=77))
        raise AssertionError(f"Unexpected statement: {sql}")


@pytest.mark.anyio
async def test_customer_telegram_connect_link_contains_signed_start_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_username", "SoulcutsBot")

    response = await messaging_routes.get_customer_telegram_connect_link(123, object(), FakeSession())
    token = str(response["connect_link"]).split("start=", maxsplit=1)[1]

    assert response["connect_link"].startswith("https://t.me/SoulcutsBot?start=")
    assert len(token) <= 64
    assert messaging_routes._customer_id_from_connect_token(token) == 123


@pytest.mark.anyio
async def test_upsert_telegram_contact_links_customer_by_shared_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_preference(session, customer_id: int):  # noqa: ANN001
        assert customer_id == 77
        return None

    fake_service = SimpleNamespace(get_preference=fake_get_preference)
    monkeypatch.setattr(messaging_routes, "service", fake_service)
    session = FakeContactSession()
    update = {
        "update_id": 101,
        "message": {
            "chat": {"id": 987654321},
            "from": {
                "id": 555,
                "username": "soulcuts_client",
                "first_name": "Ivan",
                "last_name": "Petrenko",
                "language_code": "uk",
            },
            "contact": {
                "phone_number": "050 111 22 33",
            },
        },
    }

    contact = await messaging_routes._upsert_telegram_contact_from_update(session, update)

    assert isinstance(contact, TelegramContact)
    assert contact.chat_id == "987654321"
    assert contact.telegram_user_id == "555"
    assert contact.username == "soulcuts_client"
    assert contact.first_name == "Ivan"
    assert contact.last_name == "Petrenko"
    assert contact.language_code == "uk"
    assert contact.phone == "050 111 22 33"
    assert contact.linked_customer_id == 77
    assert contact.last_update_id == 101
    assert contact.raw_update == update
    assert any(isinstance(value, ClientCommunicationPreference) for value in session.added)
    lookup_values: list[object] = []
    for value in session.customer_lookup_params.values():
        if isinstance(value, list):
            lookup_values.extend(value)
        else:
            lookup_values.append(value)
    assert "+380501112233" in lookup_values


@pytest.mark.anyio
async def test_telegram_webhook_saves_customer_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service = FakeMessagingService()
    FakeTelegramMessageProvider.sent = []
    saved_updates: list[dict] = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        saved_updates.append(update)
        return SimpleNamespace(linked_customer_id=None)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "service", fake_service)
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
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
    assert len(saved_updates) == 1
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

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return None

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "public_site_url", "https://soulcuts.com.ua")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
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

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return None

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "public_site_url", "https://soulcuts.com.ua")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
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
    saved_updates: list[dict] = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        saved_updates.append(update)
        return SimpleNamespace(linked_customer_id=None)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "public_site_url", "https://soulcuts.com.ua")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
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
    assert len(saved_updates) == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Ця команда поки недоступна в Telegram. "
            "Для запису скористайтесь онлайн-формою: https://soulcuts.com.ua/#booking",
        )
    ]
