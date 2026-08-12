from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.models.messaging import MessageChannel
from app.services.messaging import SmsMessageProvider
from app.services.sms import SmsDeliveryStatus, SmsSendResult, SmsService


class RecordingSmsService(SmsService):
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.urls: list[str] = []
        self.status_response: dict = {"success_request": {"info": {}}}

    def _post_json(self, url: str, payload: dict, headers: dict[str, str]) -> dict:
        self.urls.append(url)
        self.payloads.append(payload)
        if url.endswith("/sms/status"):
            return self.status_response
        return {"success_request": {"info": {"1": payload["phone"][0]}}}


class AcceptedSmsService(SmsService):
    async def send_message(self, phone: str, body: str, **_: object) -> SmsSendResult:
        return SmsSendResult(
            provider_message_id="smsclub-123",
            raw_response={"success_request": {"info": {"smsclub-123": phone}}},
        )


@pytest.mark.anyio
async def test_smsclub_message_uses_default_sender_name_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "token")
    monkeypatch.setattr(settings, "sms_sender_name", "")
    sms = RecordingSmsService()

    result = await sms.send_message("+380960381511", "Soul Cuts: test")

    assert sms.payloads == [
        {
            "phone": ["380960381511"],
            "message": "Soul Cuts: test",
            "src_addr": "Soul Cuts",
        }
    ]
    assert result.provider_message_id == "1"


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


def test_smsclub_request_serializes_all_sms_icons_as_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"success_request":{"info":{}}}'

    def urlopen(req, timeout):
        captured["data"] = req.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.services.sms.request.urlopen", urlopen)
    body = "💈 Як вам візит? ✂️ Все чудово ⭐"

    SmsService()._post_json("https://example.test", {"message": body}, {})

    assert captured["timeout"] == 10
    assert json.loads(captured["data"].decode("utf-8"))["message"] == body
    assert body.encode("utf-8") in captured["data"]
    assert b"\\ud83d" not in captured["data"]


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


@pytest.mark.anyio
async def test_smsclub_delivery_statuses_are_loaded_by_provider_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "token")
    sms = RecordingSmsService()
    sms.status_response = {
        "success_request": {
            "info": {
                "101": "ENROUTE",
                "102": "DELIVRD",
                "103": "UNDELIV",
            }
        }
    }

    statuses = await sms.get_message_statuses(["101", "102", "103"])

    assert sms.urls == [f"{settings.sms_club_base_url}/sms/status"]
    assert sms.payloads == [{"id_sms": ["101", "102", "103"]}]
    assert statuses == {
        "101": SmsDeliveryStatus.enroute,
        "102": SmsDeliveryStatus.delivered,
        "103": SmsDeliveryStatus.undeliverable,
    }


@pytest.mark.anyio
async def test_smsclub_delivery_status_request_is_limited_to_one_hundred_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    sms = RecordingSmsService()

    with pytest.raises(ValueError, match="at most 100"):
        await sms.get_message_statuses([str(index) for index in range(101)])


@pytest.mark.anyio
async def test_messaging_sms_provider_keeps_smsclub_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    provider = SmsMessageProvider(AcceptedSmsService())

    result = await provider.send_message(destination="+380960381511", body="Test")

    assert provider.channel == MessageChannel.sms
    assert result.provider_message_id == "smsclub-123"
    assert result.raw_response == {
        "provider": "smsclub",
        "accepted": True,
        "provider_message_id": "smsclub-123",
    }
