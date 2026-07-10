from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.sms import SmsService


class RecordingSmsService(SmsService):
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def _post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        self.payloads.append(payload)
        return {"success_request": {"info": {"1": payload["phone"][0]}}}


@pytest.mark.anyio
async def test_smsclub_message_uses_default_sender_name_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "token")
    monkeypatch.setattr(settings, "sms_sender_name", "")
    sms = RecordingSmsService()

    await sms.send_message("+380960381511", "Soul Cuts: test")

    assert sms.payloads == [
        {
            "phone": ["380960381511"],
            "message": "Soul Cuts: test",
            "src_addr": "Soul Cuts",
        }
    ]


@pytest.mark.anyio
async def test_smsclub_message_includes_configured_sender_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "token")
    monkeypatch.setattr(settings, "sms_sender_name", "SoulCuts")
    sms = RecordingSmsService()

    await sms.send_message("+380960381511", "Soul Cuts: test")

    assert sms.payloads == [
        {
            "phone": ["380960381511"],
            "message": "Soul Cuts: test",
            "src_addr": "SoulCuts",
        }
    ]


@pytest.mark.anyio
async def test_smsclub_otp_uses_soul_cuts_sender_and_login_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "token")
    monkeypatch.setattr(settings, "sms_sender_name", "Soul Cuts")
    monkeypatch.setattr(settings, "sms_otp_template", "Ваш код для входу в Soul Cuts: {code}. Нікому його не повідомляйте.")
    monkeypatch.setattr(settings, "otp_code_ttl_minutes", 10)
    sms = RecordingSmsService()

    await sms.send_otp_code("+380960381511", "123456")

    assert sms.payloads == [
        {
            "phone": ["380960381511"],
            "message": "Ваш код для входу в Soul Cuts: 123456. Нікому його не повідомляйте.",
            "src_addr": "Soul Cuts",
            "lifetime": 10,
        }
    ]
