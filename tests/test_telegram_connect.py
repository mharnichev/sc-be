from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app.api.v1.routes import messaging as messaging_routes
from app.core.config import settings
from app.models.booking import BookingSource
from app.models.messaging import ClientCommunicationPreference, ConsentStatus, TelegramContact
from app.services import messaging as messaging_service
from app.services.messaging import ProviderSendResult, TelegramMessageProvider


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
    sent: list[tuple] = []
    sent_photos: list[tuple] = []
    answered_callbacks: list[str] = []

    async def send_message(
        self,
        *,
        destination: str,
        body: str,
        reply_markup: dict | None = None,
    ) -> ProviderSendResult:
        if reply_markup is None:
            self.sent.append((destination, body))
        else:
            self.sent.append((destination, body, reply_markup))
        return ProviderSendResult(provider_message_id="1", raw_response={"ok": True})

    async def send_photo(
        self,
        *,
        destination: str,
        photo_url: str | None = None,
        photo_path: Path | None = None,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> ProviderSendResult:
        photo_source = photo_path if photo_path is not None else photo_url
        self.sent_photos.append((destination, photo_source, caption, reply_markup))
        return ProviderSendResult(provider_message_id="1", raw_response={"ok": True})

    async def answer_callback_query(self, *, callback_query_id: str, text: str | None = None) -> dict:
        self.answered_callbacks.append(callback_query_id)
        return {"ok": True}


class FailingTelegramMessageProvider(FakeTelegramMessageProvider):
    async def send_message(
        self,
        *,
        destination: str,
        body: str,
        reply_markup: dict | None = None,
    ) -> ProviderSendResult:
        raise RuntimeError("Forbidden: bot was blocked by the user")

    async def answer_callback_query(self, *, callback_query_id: str, text: str | None = None) -> dict:
        raise RuntimeError("Bad Request: query is too old")


class FakeScalarResult:
    def __init__(self, value) -> None:  # noqa: ANN001
        self.value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.value


class FakeScalarListResult:
    def __init__(self, values: list) -> None:  # noqa: ANN001
        self.values = values

    def all(self) -> list:
        return self.values


class FakeExecuteListResult:
    def __init__(self, values: list) -> None:  # noqa: ANN001
        self.values = values

    def scalars(self) -> FakeScalarListResult:
        return FakeScalarListResult(self.values)


class FakeRowsResult:
    def __init__(self, rows: list) -> None:  # noqa: ANN001
        self.rows = rows

    def all(self) -> list:
        return self.rows


class FakeScalarOneResult:
    def __init__(self, value) -> None:  # noqa: ANN001
        self.value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.value


class FakeMastersSession:
    def __init__(self, masters: list, review_rows: list | None = None) -> None:  # noqa: ANN001
        self.masters = masters
        self.review_rows = review_rows or []

    async def execute(self, statement):  # noqa: ANN001, ANN201
        if "master_reviews" in str(statement):
            return FakeRowsResult(self.review_rows)
        return FakeExecuteListResult(self.masters)


class FakeMasterSelectionSession:
    def __init__(self, master) -> None:  # noqa: ANN001
        self.master = master
        self.bot_session = None
        self.added: list[object] = []
        self.commit_count = 0
        self.flushed = False

    def add(self, value: object) -> None:
        self.added.append(value)
        if value.__class__.__name__ == "TelegramBotSession":
            self.bot_session = value

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        if "masters" in sql:
            return FakeScalarOneResult(self.master)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.commit_count += 1


class FakeServicesSession:
    def __init__(self, bot_session, services: list) -> None:  # noqa: ANN001
        self.bot_session = bot_session
        self.services = services
        self.commit_count = 0

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        if "barber_services" in sql:
            return FakeExecuteListResult(self.services)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def commit(self) -> None:
        self.commit_count += 1


class FakeServiceSelectionSession:
    def __init__(self, bot_session, service_item) -> None:  # noqa: ANN001
        self.bot_session = bot_session
        self.service_item = service_item
        self.commit_count = 0

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        if "barber_services" in sql:
            return FakeScalarOneResult(self.service_item)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def commit(self) -> None:
        self.commit_count += 1


class FakeDateTimeSession:
    def __init__(self, bot_session) -> None:  # noqa: ANN001
        self.bot_session = bot_session
        self.commit_count = 0

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def commit(self) -> None:
        self.commit_count += 1


class FakeBookingService:
    def __init__(self) -> None:
        self.payload = None
        self.source = None

    async def create_public_booking(self, session, payload, *, source=None):  # noqa: ANN001, ANN201
        self.payload = payload
        self.source = source
        return SimpleNamespace(id=73723)


class FakeTimeSelectionSession:
    def __init__(self, bot_session, master, services: list) -> None:  # noqa: ANN001
        self.bot_session = bot_session
        self.master = master
        self.services = services
        self.commit_count = 0

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        if "masters" in sql:
            return FakeScalarOneResult(self.master)
        if "barber_services" in sql:
            return FakeExecuteListResult(self.services)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def commit(self) -> None:
        self.commit_count += 1


class FakeViewBookingsSession:
    def __init__(self, bot_session, bookings: list) -> None:  # noqa: ANN001
        self.bot_session = bot_session
        self.bookings = bookings
        self.commit_count = 0

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        if "bookings" in sql:
            return FakeExecuteListResult(self.bookings)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def commit(self) -> None:
        self.commit_count += 1


class FakeCancelBookingSession:
    def __init__(self, bot_session, booking) -> None:  # noqa: ANN001
        self.bot_session = bot_session
        self.booking = booking
        self.commit_count = 0

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        if "bookings" in sql:
            return FakeScalarOneResult(self.booking)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def commit(self) -> None:
        self.commit_count += 1


class FakeNewBookingSession:
    def __init__(self, bot_session) -> None:  # noqa: ANN001
        self.bot_session = bot_session
        self.added: list[object] = []
        self.commit_count = 0
        self.flushed = False

    def add(self, value: object) -> None:
        self.added.append(value)
        if value.__class__.__name__ == "TelegramBotSession":
            self.bot_session = value

    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_bot_sessions" in sql:
            return FakeScalarOneResult(self.bot_session)
        raise AssertionError(f"Unexpected statement: {sql}")

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.commit_count += 1


class FakeDuplicateUpdateSession:
    async def execute(self, statement):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "telegram_contacts" in sql:
            return FakeScalarResult(101)
        raise AssertionError(f"Unexpected statement: {sql}")


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


class FakeMasterTelegramConnectSession:
    def __init__(self, master) -> None:  # noqa: ANN001
        self.master = master
        self.commit_count = 0

    async def get(self, model, entity_id):  # noqa: ANN001
        assert model is messaging_routes.Master
        assert entity_id == self.master.id
        return self.master

    async def commit(self) -> None:
        self.commit_count += 1


@pytest.mark.anyio
async def test_customer_telegram_connect_link_contains_signed_start_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_username", "SoulcutsBot")

    response = await messaging_routes.get_customer_telegram_connect_link(123, object(), FakeSession())
    token = str(response["connect_link"]).split("start=", maxsplit=1)[1]

    assert response["connect_link"].startswith("https://t.me/SoulcutsBot?start=")
    assert len(token) <= 64
    assert messaging_routes._customer_id_from_connect_token(token) == 123


@pytest.mark.anyio
async def test_master_telegram_connect_link_contains_signed_start_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_username", "SoulcutsBot")
    master = SimpleNamespace(id=55, telegram_chat_id=None)

    response = await messaging_routes.get_my_master_telegram_connect_link(master)
    token = str(response["connect_link"]).split("start=", maxsplit=1)[1]

    assert response == {
        "master_id": 55,
        "bot_username": "SoulcutsBot",
        "connect_link": response["connect_link"],
        "expires_in_days": messaging_routes.TELEGRAM_CUSTOMER_CONNECT_TOKEN_DAYS,
        "telegram_connected": False,
    }
    assert response["connect_link"].startswith("https://t.me/SoulcutsBot?start=")
    assert len(token) <= 64
    assert messaging_routes._master_id_from_connect_token(token) == 55


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
async def test_telegram_webhook_replies_to_start_with_share_contact_button(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "message": {
                "text": "/start",
                "chat": {"id": 987654321},
            }
        }
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=FakeSession(),
    )

    assert response == {"ok": True, "handled": True, "action": "start_share_contact"}
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            (
                'Вітаємо, тепер записатися стало простіше! '
                'Для початку натисніть "Поділитись контактом" унизу.'
            ),
            {
                "keyboard": [[{"text": "Поділитись контактом", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_ignores_duplicate_update(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        raise AssertionError("duplicate update should not be upserted")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "update_id": 101,
            "message": {
                "text": "Забронювати",
                "chat": {"id": 987654321},
            },
        }
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=FakeDuplicateUpdateSession(),
    )

    assert response == {"ok": True, "handled": True, "action": "duplicate_update"}
    assert FakeTelegramMessageProvider.sent == []


@pytest.mark.anyio
async def test_telegram_webhook_replies_to_contact_with_booking_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []
    saved_updates: list[dict] = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        saved_updates.append(update)
        return SimpleNamespace(linked_customer_id=77)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "message": {
                "chat": {"id": 987654321},
                "contact": {
                    "phone_number": "050 111 22 33",
                },
            }
        }
    )
    session = FakeSession()

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "contact_saved"}
    assert session.committed is False
    assert len(saved_updates) == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Контакт збережено.\n\nБудь ласка, оберіть потрібну дію:",
            {
                "keyboard": [
                    [{"text": "Майстер"}, {"text": "Послуги"}],
                    [{"text": "Дата і час"}, {"text": "Скасувати"}],
                ],
                "resize_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_replies_to_master_action_with_available_masters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.sent_photos = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "message": {
                "text": "Майстер",
                "chat": {"id": 987654321},
            }
        }
    )
    session = FakeMastersSession(
        [
            SimpleNamespace(
                id=10,
                full_name="Глеб",
                full_name_uk="Глеб",
                position_uk="",
                phone="+380661478027",
                photo_url=None,
                avatar_url=None,
            ),
            SimpleNamespace(
                id=20,
                full_name="Для клієнтів Soulcuts",
                full_name_uk="Для клієнтів Soulcuts",
                position_uk="Мастер",
                phone="+380636995730",
                photo_url=None,
                avatar_url=None,
            ),
        ]
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "list_masters"}
    assert FakeTelegramMessageProvider.sent_photos == []
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Глеб - \n\n+380661478027",
            {
                "inline_keyboard": [
                    [{"text": "Обрати Глеб", "callback_data": "select_master:10"}],
                ]
            },
        ),
        (
            "987654321",
            "Для клієнтів Soulcuts - Мастер\n\n+380636995730",
            {
                "inline_keyboard": [
                    [{"text": "Обрати Для клієнтів Soulcuts", "callback_data": "select_master:20"}],
                ]
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_requires_contact_before_booking_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(linked_customer_id=None, phone=None)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "message": {
                "text": "Майстер",
                "chat": {"id": 987654321},
            }
        }
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=FakeSession(),
    )

    assert response == {"ok": True, "handled": True, "action": "contact_required"}
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            (
                'Вітаємо, тепер записатися стало простіше! '
                'Для початку натисніть "Поділитись контактом" унизу.'
            ),
            {
                "keyboard": [[{"text": "Поділитись контактом", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_master_list_uploads_master_photo_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PIL import Image

    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.sent_photos = []
    upload_dir = tmp_path / "uploads"
    source_path = upload_dir / "barbers" / "gleb.webp"
    source_path.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), (20, 40, 60)).save(source_path, "WEBP")

    monkeypatch.setattr(settings, "upload_dir", str(upload_dir))
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.soulcuts.com.ua")
    session = FakeMastersSession(
        [
            SimpleNamespace(
                id=10,
                full_name="Глеб",
                full_name_uk="Глеб Гарницев",
                position_uk="Майстер",
                phone="+380661478027",
                photo_url="/media/barbers/gleb.webp",
                photo_upload=SimpleNamespace(id=1, file_path=str(source_path)),
                avatar_url=None,
                avatar_upload=None,
            )
        ],
        review_rows=[(10, Decimal("4.86"), 12)],
    )

    await messaging_routes._send_master_list(FakeTelegramMessageProvider(), session, "987654321")

    assert FakeTelegramMessageProvider.sent == []
    assert len(FakeTelegramMessageProvider.sent_photos) == 1
    destination, photo_source, caption, reply_markup = FakeTelegramMessageProvider.sent_photos[0]
    assert destination == "987654321"
    assert isinstance(photo_source, Path)
    assert photo_source.is_file()
    with Image.open(photo_source) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 64)
    assert caption == "Глеб Гарницев - Майстер\n⭐ 4.9 · 12 відгуків\n\n+380661478027"
    assert reply_markup == {
        "inline_keyboard": [
            [{"text": "Обрати Глеб Гарницев", "callback_data": "select_master:10"}],
        ]
    }


@pytest.mark.anyio
async def test_telegram_master_list_retries_photo_by_url_when_file_upload_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PIL import Image

    class FileUploadFailingProvider(FakeTelegramMessageProvider):
        async def send_photo(
            self,
            *,
            destination: str,
            photo_url: str | None = None,
            photo_path: Path | None = None,
            caption: str | None = None,
            reply_markup: dict | None = None,
        ) -> ProviderSendResult:
            if photo_path is not None:
                raise RuntimeError("local upload failed")
            return await super().send_photo(
                destination=destination,
                photo_url=photo_url,
                caption=caption,
                reply_markup=reply_markup,
            )

    FileUploadFailingProvider.sent = []
    FileUploadFailingProvider.sent_photos = []
    upload_dir = tmp_path / "uploads"
    source_path = upload_dir / "barbers" / "gleb.webp"
    source_path.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), (20, 40, 60)).save(source_path, "WEBP")

    monkeypatch.setattr(settings, "upload_dir", str(upload_dir))
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.soulcuts.com.ua")
    master = SimpleNamespace(
        id=10,
        full_name="Глеб",
        full_name_uk="Глеб Гарницев",
        position_uk="Майстер",
        phone="+380661478027",
        photo_url="/media/barbers/gleb.webp",
        photo_upload=SimpleNamespace(id=1, file_path=str(source_path)),
        avatar_url=None,
        avatar_upload=None,
    )

    await messaging_routes._send_master_list(
        FileUploadFailingProvider(),
        FakeMastersSession([master]),
        "987654321",
    )

    assert FileUploadFailingProvider.sent == []
    assert FileUploadFailingProvider.sent_photos[0][1] == (
        "https://api.soulcuts.com.ua/api/v1/public/telegram/master-photo/10.jpg"
    )


@pytest.mark.anyio
async def test_telegram_provider_sends_local_photo_as_multipart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    photo_path = tmp_path / "master.jpg"
    photo_bytes = b"jpeg-photo-content"
    photo_path.write_bytes(photo_bytes)
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001, ANN204
            return False

        def read(self) -> bytes:
            return b'{"ok": true, "result": {"message_id": 42}}'

    def fake_urlopen(req, timeout):  # noqa: ANN001, ANN202
        captured["request"] = req
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    monkeypatch.setattr(settings, "telegram_api_base_url", "https://telegram.example")
    monkeypatch.setattr(messaging_service.request, "urlopen", fake_urlopen)

    reply_markup = {
        "inline_keyboard": [[{"text": "Обрати Глеб", "callback_data": "select_master:10"}]],
    }
    result = await TelegramMessageProvider().send_photo(
        destination="987654321",
        photo_path=photo_path,
        caption="Глеб - Майстер",
        reply_markup=reply_markup,
    )

    req = captured["request"]
    content_type = req.get_header("Content-type")
    assert result.provider_message_id == "42"
    assert req.full_url == "https://telegram.example/bottoken/sendPhoto"
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"' in req.data
    assert b"987654321" in req.data
    assert b'name="caption"' in req.data
    assert "Глеб - Майстер".encode() in req.data
    assert b'name="reply_markup"' in req.data
    assert json.dumps(reply_markup, ensure_ascii=False).encode() in req.data
    assert b'name="photo"; filename="master.jpg"' in req.data
    assert photo_bytes in req.data


@pytest.mark.anyio
async def test_telegram_master_photo_endpoint_serves_cached_jpeg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PIL import Image

    upload_dir = tmp_path / "uploads"
    source_path = upload_dir / "barbers" / "gleb.webp"
    source_path.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), (20, 40, 60)).save(source_path, "WEBP")

    monkeypatch.setattr(settings, "upload_dir", str(upload_dir))
    master = SimpleNamespace(
        id=10,
        is_active=True,
        photo_url="/media/barbers/gleb.webp",
        avatar_url=None,
        photo_upload=SimpleNamespace(id=1, file_path=str(source_path)),
        avatar_upload=None,
    )

    class FakeMasterPhotoSession:
        async def execute(self, statement):  # noqa: ANN001, ANN201
            return FakeScalarOneResult(master)

    response = await messaging_routes.get_telegram_master_photo(10, session=FakeMasterPhotoSession())
    cached_path = Path(response.path)

    assert response.media_type == "image/jpeg"
    assert cached_path.is_file()
    with Image.open(cached_path) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 64)


@pytest.mark.anyio
async def test_telegram_webhook_saves_selected_master_and_replies_with_next_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(id=30, linked_customer_id=77)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "select_master:10",
                "message": {
                    "chat": {"id": 987654321},
                },
            }
        }
    )
    session = FakeMasterSelectionSession(SimpleNamespace(id=10, full_name="Глеб", full_name_uk="Глеб"))

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "select_master"}
    assert FakeTelegramMessageProvider.answered_callbacks == ["callback-1"]
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Ви обрали майстра: Глеб.\n\nБудь ласка, оберіть потрібну дію:",
            {
                "keyboard": [
                    [{"text": "Послуги"}, {"text": "Дата та час"}],
                    [{"text": "Скасувати"}],
                ],
                "resize_keyboard": True,
            },
        )
    ]
    assert session.bot_session.chat_id == "987654321"
    assert session.bot_session.telegram_contact_id == 30
    assert session.bot_session.linked_customer_id == 77
    assert session.bot_session.selected_master_id == 10
    assert session.bot_session.state == "master_selected"
    assert session.flushed is True
    assert session.commit_count == 1


@pytest.mark.anyio
async def test_telegram_webhook_replies_to_services_action_with_master_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "message": {
                "text": "Послуги",
                "chat": {"id": 987654321},
            }
        }
    )
    bot_session = SimpleNamespace(selected_master_id=10, payload_json={}, state="master_selected")
    session = FakeServicesSession(
        bot_session,
        [
            SimpleNamespace(id=100, name="Haircut", title_uk="Стрижка", price=1200),
            SimpleNamespace(id=200, name="Army", title_uk="Стрижка ЗСУ", price=0),
            SimpleNamespace(id=300, name="Beard", title_uk="Стрижка бороди", price=800),
        ],
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "list_services"}
    assert bot_session.state == "selecting_services"
    assert bot_session.payload_json == {"selected_service_ids": []}
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Оберіть одну або більше послуг:",
            {
                "inline_keyboard": [
                    [{"text": "🙂 Стрижка · 1200 грн", "callback_data": "select_service:100"}],
                    [{"text": "🙂 Стрижка ЗСУ · 0 грн", "callback_data": "select_service:200"}],
                    [{"text": "🧔 Стрижка бороди · 800 грн", "callback_data": "select_service:300"}],
                ]
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_adds_service_to_multi_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "select_service:200",
                "message": {
                    "chat": {"id": 987654321},
                },
            }
        }
    )
    bot_session = SimpleNamespace(
        selected_master_id=10,
        selected_service_id=100,
        payload_json={"selected_service_ids": [100]},
        state="selecting_services",
    )
    session = FakeServiceSelectionSession(
        bot_session,
        SimpleNamespace(id=200, master_id=10, name="Beard", title_uk="Борода"),
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "select_service"}
    assert FakeTelegramMessageProvider.answered_callbacks == ["callback-1"]
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Будь ласка, оберіть потрібну дію:",
            {
                "keyboard": [
                    [{"text": "Дата та час"}],
                    [{"text": "Скасувати"}],
                ],
                "resize_keyboard": True,
            },
        )
    ]
    assert bot_session.selected_service_id == 100
    assert bot_session.payload_json == {"selected_service_ids": [100, 200]}
    assert bot_session.state == "selecting_services"
    assert session.commit_count == 1


@pytest.mark.anyio
async def test_telegram_webhook_replies_to_date_time_action_with_visit_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    async def fake_available_visit_dates(session, *, master_id: int, service_ids: list[int]):  # noqa: ANN001
        assert master_id == 10
        assert service_ids == [100, 200]
        return [date(2026, 6, 21), date(2026, 6, 24)]

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    monkeypatch.setattr(messaging_routes, "_available_visit_dates", fake_available_visit_dates)
    request = FakeRequest(
        {
            "message": {
                "text": "Дата та час",
                "chat": {"id": 987654321},
            }
        }
    )
    bot_session = SimpleNamespace(
        selected_master_id=10,
        payload_json={"selected_service_ids": [100, 200]},
        state="selecting_services",
    )
    session = FakeDateTimeSession(bot_session)

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "list_visit_dates"}
    assert bot_session.state == "selecting_date"
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Оберіть дату візиту",
            {
                "inline_keyboard": [
                    [{"text": "21.06 Нд", "callback_data": "select_date:2026-06-21"}],
                    [{"text": "24.06 Ср", "callback_data": "select_date:2026-06-24"}],
                ]
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_replies_to_date_selection_with_time_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    async def fake_available_visit_slots(session, *, master_id: int, service_ids: list[int], visit_date: date):  # noqa: ANN001
        assert master_id == 10
        assert service_ids == [100, 200]
        assert visit_date == date(2026, 6, 21)
        return [
            SimpleNamespace(start_at=datetime(2026, 6, 21, 10, 0, tzinfo=messaging_routes.KYIV_TZ)),
            SimpleNamespace(start_at=datetime(2026, 6, 21, 10, 30, tzinfo=messaging_routes.KYIV_TZ)),
        ]

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    monkeypatch.setattr(messaging_routes, "_available_visit_slots", fake_available_visit_slots)
    request = FakeRequest(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "select_date:2026-06-21",
                "message": {
                    "chat": {"id": 987654321},
                },
            }
        }
    )
    bot_session = SimpleNamespace(
        selected_master_id=10,
        payload_json={"selected_service_ids": [100, 200]},
        state="selecting_date",
    )
    session = FakeDateTimeSession(bot_session)

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "select_date"}
    assert FakeTelegramMessageProvider.answered_callbacks == ["callback-1"]
    assert bot_session.state == "selecting_time"
    assert bot_session.payload_json == {
        "selected_service_ids": [100, 200],
        "selected_visit_date": "2026-06-21",
    }
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Оберіть час візиту",
            {
                "inline_keyboard": [
                    [{"text": "10:00", "callback_data": "select_time:2026-06-21T10:00:00"}],
                    [{"text": "10:30", "callback_data": "select_time:2026-06-21T10:30:00"}],
                ]
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_replies_to_time_selection_with_booking_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "select_time:2026-06-21T10:00:00",
                "message": {
                    "chat": {"id": 987654321},
                },
            }
        }
    )
    bot_session = SimpleNamespace(
        selected_master_id=10,
        payload_json={"selected_service_ids": [100, 200], "selected_visit_date": "2026-06-21"},
        state="selecting_time",
    )
    session = FakeTimeSelectionSession(
        bot_session,
        SimpleNamespace(id=10, full_name="Глеб", full_name_uk="Глеб", position_uk=""),
        [
            SimpleNamespace(id=100, title_uk="Стрижка", name="Haircut", price=1200),
            SimpleNamespace(id=200, title_uk="Стрижка бороди", name="Beard", price=800),
        ],
    )

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "select_time"}
    assert FakeTelegramMessageProvider.answered_callbacks == ["callback-1"]
    assert bot_session.state == "ready_to_book"
    assert bot_session.payload_json == {
        "selected_service_ids": [100, 200],
        "selected_visit_date": "2026-06-21",
        "selected_visit_time": "2026-06-21T10:00:00+03:00",
    }
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            (
                "Ви обрали час: 21.06 10:00, Нд.\n\n\n"
                "Деталі запису\n\n"
                "неділя 21 червня - 10:00\n\n"
                "Глеб - \n\n"
                "Стрижка. Майстер Глеб (1200 грн)\n"
                "Стрижка бороди. Майстер Глеб (800 грн)\n\n\n"
                "Будь ласка, оберіть потрібну дію:"
            ),
            {
                "keyboard": [
                    [{"text": "Забронювати"}],
                    [{"text": "Скасувати"}],
                ],
                "resize_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_creates_booking_on_book_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []
    fake_booking_service = FakeBookingService()

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(
            linked_customer_id=None,
            first_name="Ivan",
            last_name="Petrenko",
            username="ivan",
            phone="050 111 22 33",
        )

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    monkeypatch.setattr(messaging_routes, "booking_service_layer", fake_booking_service)
    request = FakeRequest(
        {
            "message": {
                "text": "Забронювати",
                "chat": {"id": 987654321},
            }
        }
    )
    bot_session = SimpleNamespace(
        selected_master_id=10,
        payload_json={
            "selected_service_ids": [100, 200],
            "selected_visit_date": "2026-06-21",
            "selected_visit_time": "2026-06-21T10:00:00+03:00",
        },
        state="ready_to_book",
    )
    session = FakeDateTimeSession(bot_session)

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "book"}
    assert fake_booking_service.payload.master_id == 10
    assert fake_booking_service.payload.service_ids == [100, 200]
    assert fake_booking_service.payload.customer_name == "Ivan Petrenko"
    assert fake_booking_service.payload.customer_phone == "050 111 22 33"
    assert fake_booking_service.payload.start_at == datetime(2026, 6, 21, 10, 0, tzinfo=messaging_routes.KYIV_TZ)
    assert fake_booking_service.source == BookingSource.telegram
    assert bot_session.state == "booked"
    assert bot_session.payload_json == {
        "selected_service_ids": [100, 200],
        "selected_visit_date": "2026-06-21",
        "selected_visit_time": "2026-06-21T10:00:00+03:00",
        "booking_id": 73723,
    }
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Запис здійснено успішно! Номер замовлення: 73723.\n\n\nБудь ласка, оберіть потрібну дію:",
            {
                "keyboard": [
                    [{"text": "Новий запис"}],
                    [{"text": "Перегляд записів"}],
                ],
                "resize_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_replies_with_customer_bookings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(linked_customer_id=77)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "message": {
                "text": "Перегляд записів",
                "chat": {"id": 987654321},
            }
        }
    )
    bot_session = SimpleNamespace(
        linked_customer_id=77,
        payload_json={"booking_id": 73723},
        state="booked",
    )
    booking = SimpleNamespace(
        id=73723,
        master=SimpleNamespace(
            full_name="Технічний календар",
            full_name_uk="Технічний календар",
        ),
        redirected_from_master=SimpleNamespace(full_name="Глеб", full_name_uk="Глеб"),
        services=[
            SimpleNamespace(id=100, title_uk="Стрижка", name="Haircut", price=1200),
            SimpleNamespace(id=200, title_uk="Стрижка бороди", name="Beard", price=800),
        ],
        start_at=datetime(2026, 6, 21, 10, 0, tzinfo=messaging_routes.KYIV_TZ),
        end_at=datetime(2026, 6, 21, 12, 0, tzinfo=messaging_routes.KYIV_TZ),
        customer_comment="",
    )
    session = FakeViewBookingsSession(bot_session, [booking])

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "view_bookings"}
    assert bot_session.state == "viewing_bookings"
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            (
                "Ім‘я майстра: Глеб\n"
                "Послуги: Стрижка. Майстер Глеб (1200 грн), "
                "Стрижка бороди. Майстер Глеб (800 грн)\n"
                "Час зустрічі: неділя 21 червня 10:00 - 12:00\n"
                "Коментар: \n"
                "Загальна вартість: 2000 грн"
            ),
            {
                "inline_keyboard": [
                    [{"text": "Скасувати", "callback_data": "cancel_booking:73723"}],
                ]
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_cancels_customer_booking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(linked_customer_id=77)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "cancel_booking:73723",
                "message": {
                    "chat": {"id": 987654321},
                },
            }
        }
    )
    bot_session = SimpleNamespace(
        linked_customer_id=77,
        payload_json={"booking_id": 73723},
        state="viewing_bookings",
    )
    booking = SimpleNamespace(
        id=73723,
        customer_id=77,
        customer_name="Ivan Petrenko",
        status=messaging_routes.BookingStatus.confirmed,
        start_at=datetime(2026, 6, 21, 10, 0, tzinfo=messaging_routes.KYIV_TZ),
        end_at=datetime(2026, 6, 21, 12, 0, tzinfo=messaging_routes.KYIV_TZ),
        cancelled_at=None,
        completed_at=None,
        master=SimpleNamespace(telegram_chat_id="111"),
        services=[SimpleNamespace(title_uk="Стрижка", name="Haircut")],
        service=None,
    )
    session = FakeCancelBookingSession(bot_session, booking)
    background_tasks = BackgroundTasks()

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        background_tasks=background_tasks,
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "cancel_booking"}
    assert FakeTelegramMessageProvider.answered_callbacks == ["callback-1"]
    assert booking.status == messaging_routes.BookingStatus.cancelled
    assert booking.cancelled_at is not None
    assert booking.completed_at is None
    assert bot_session.state == "booking_cancelled"
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        ("987654321", "Запис скасовано."),
    ]
    assert len(background_tasks.tasks) == 1
    notification = background_tasks.tasks[0].args[0]
    assert notification.telegram_chat_id == "111"
    assert notification.service_name == "Стрижка"
    assert notification.customer_name == "Ivan Petrenko"


def test_telegram_booking_notifications_are_scheduled_for_future_booking() -> None:
    background_tasks = BackgroundTasks()
    booking = SimpleNamespace(
        id=73723,
        master=SimpleNamespace(
            full_name="Глеб",
            full_name_uk="Глеб",
            email="gleb@example.test",
            telegram_chat_id="111",
        ),
        services=[
            SimpleNamespace(id=100, title_uk="Стрижка", name="Haircut", price=1200),
            SimpleNamespace(id=200, title_uk="Борода", name="Beard", price=800),
        ],
        customer_name="Ivan Petrenko",
        customer_phone="+380501112233",
        customer_comment="",
        start_at=datetime(2099, 6, 21, 10, 0, tzinfo=messaging_routes.KYIV_TZ),
        end_at=datetime(2099, 6, 21, 12, 0, tzinfo=messaging_routes.KYIV_TZ),
    )

    messaging_routes._schedule_booking_notifications(background_tasks, booking)

    assert len(background_tasks.tasks) == 3


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
async def test_telegram_webhook_saves_master_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []
    master = SimpleNamespace(id=55, full_name="Глеб", full_name_uk="Глеб", telegram_chat_id=None)
    session = FakeMasterTelegramConnectSession(master)

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(linked_customer_id=None)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    token = messaging_routes._master_connect_token(55)
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
        session=session,
    )

    assert response == {"ok": True, "handled": True, "master_id": 55, "telegram_chat_id": "987654321"}
    assert master.telegram_chat_id == "987654321"
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Telegram підключено для майстра Глеб. Тепер ви отримуватимете сповіщення про нові записи.",
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_starts_new_booking_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(id=30, linked_customer_id=77, phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
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
    bot_session = SimpleNamespace(
        telegram_contact_id=30,
        linked_customer_id=77,
        selected_master_id=10,
        selected_service_id=100,
        payload_json={"selected_service_ids": [100], "booking_id": 73723},
        state="booked",
    )
    session = FakeNewBookingSession(bot_session)

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "new_booking_start"}
    assert bot_session.selected_master_id is None
    assert bot_session.selected_service_id is None
    assert bot_session.payload_json == {}
    assert bot_session.state == "booking_started"
    assert session.commit_count == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Будь ласка, оберіть потрібну дію:",
            {
                "keyboard": [
                    [{"text": "Майстер"}, {"text": "Послуги"}],
                    [{"text": "Дата і час"}, {"text": "Скасувати"}],
                ],
                "resize_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_starts_new_booking_flow_for_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []
    FakeTelegramMessageProvider.answered_callbacks = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(id=30, linked_customer_id=77, phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
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
    bot_session = SimpleNamespace(
        telegram_contact_id=30,
        linked_customer_id=77,
        selected_master_id=10,
        selected_service_id=100,
        payload_json={"selected_service_ids": [100]},
        state="booked",
    )
    session = FakeNewBookingSession(bot_session)

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "new_booking_start"}
    assert FakeTelegramMessageProvider.answered_callbacks == ["callback-1"]
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Будь ласка, оберіть потрібну дію:",
            {
                "keyboard": [
                    [{"text": "Майстер"}, {"text": "Послуги"}],
                    [{"text": "Дата і час"}, {"text": "Скасувати"}],
                ],
                "resize_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_cancels_draft_booking_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeTelegramMessageProvider.sent = []

    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(phone="050 111 22 33")

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FakeTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "message": {
                "text": "Скасувати",
                "chat": {"id": 987654321},
            }
        }
    )
    bot_session = SimpleNamespace(
        selected_master_id=10,
        selected_service_id=100,
        payload_json={"selected_service_ids": [100]},
        state="ready_to_book",
    )
    session = FakeDateTimeSession(bot_session)

    response = await messaging_routes.telegram_webhook(
        request,
        x_telegram_bot_api_secret_token="secret",
        session=session,
    )

    assert response == {"ok": True, "handled": True, "action": "cancel_draft"}
    assert bot_session.selected_master_id is None
    assert bot_session.selected_service_id is None
    assert bot_session.payload_json == {}
    assert bot_session.state == "draft_cancelled"
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            "Дію скасовано.\n\nБудь ласка, оберіть потрібну дію:",
            {
                "keyboard": [
                    [{"text": "Новий запис"}],
                    [{"text": "Перегляд записів"}],
                ],
                "resize_keyboard": True,
            },
        )
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

    assert response == {"ok": True, "handled": True, "action": "contact_required"}
    assert len(saved_updates) == 1
    assert FakeTelegramMessageProvider.sent == [
        (
            "987654321",
            (
                'Вітаємо, тепер записатися стало простіше! '
                'Для початку натисніть "Поділитись контактом" унизу.'
            ),
            {
                "keyboard": [[{"text": "Поділитись контактом", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            },
        )
    ]


@pytest.mark.anyio
async def test_telegram_webhook_does_not_fail_when_reply_to_legacy_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_upsert_contact(session, update):  # noqa: ANN001
        return SimpleNamespace(linked_customer_id=None)

    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(messaging_routes, "TelegramMessageProvider", FailingTelegramMessageProvider)
    monkeypatch.setattr(messaging_routes, "_upsert_telegram_contact_from_update", fake_upsert_contact)
    request = FakeRequest(
        {
            "callback_query": {
                "id": "callback-1",
                "data": "legacy_action",
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

    assert response == {"ok": True, "handled": True, "action": "contact_required"}
