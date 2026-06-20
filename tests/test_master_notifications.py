from __future__ import annotations

from datetime import datetime

import pytest

from app.core.config import settings
from app.services.booking import KYIV_TZ
from app.services.master_notifications import MasterTelegramNotificationService, NewBookingTelegram
from app.services.messaging import ProviderSendResult, TelegramMessageProvider


def new_booking_telegram() -> NewBookingTelegram:
    return NewBookingTelegram(
        booking_id=42,
        master_name="Gleb",
        telegram_chat_id="123456789",
        service_name="Haircut",
        customer_name="Ivan",
        customer_phone="+380501112233",
        customer_comment="No beard trim",
        start_at=datetime(2099, 1, 1, 10, 0, tzinfo=KYIV_TZ),
        end_at=datetime(2099, 1, 1, 11, 0, tzinfo=KYIV_TZ),
    )


class RecordingTelegramProvider(TelegramMessageProvider):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, *, destination: str, body: str, reply_markup: dict | None = None) -> ProviderSendResult:
        self.sent.append((destination, body))
        return ProviderSendResult(provider_message_id="99", raw_response={"ok": True})


@pytest.mark.anyio
async def test_new_booking_telegram_notification_is_skipped_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    provider = RecordingTelegramProvider()

    await MasterTelegramNotificationService(provider).send_new_booking_to_master(new_booking_telegram())

    assert provider.sent == []


@pytest.mark.anyio
async def test_new_booking_telegram_notification_is_sent_to_master_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    provider = RecordingTelegramProvider()

    await MasterTelegramNotificationService(provider).send_new_booking_to_master(new_booking_telegram())

    assert provider.sent == [
        (
            "123456789",
            "Йоу! Є нова праця, збирай раму! Ivan Haircut 01.01.2099 10:00",
        )
    ]


def test_new_booking_telegram_message_uses_neutral_scenario_copy() -> None:
    message = MasterTelegramNotificationService().build_new_booking_message(new_booking_telegram())

    assert message == "Йоу! Є нова праця, збирай раму! Ivan Haircut 01.01.2099 10:00"
