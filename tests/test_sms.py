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
async def test_smsclub_message_omits_empty_sender_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "token")
    monkeypatch.setattr(settings, "sms_sender_name", "")
    sms = RecordingSmsService()

    await sms.send_message("+380960381511", "Soul Cuts: test")

    assert sms.payloads == [
        {
            "phone": ["380960381511"],
            "message": "Soul Cuts: test",
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
