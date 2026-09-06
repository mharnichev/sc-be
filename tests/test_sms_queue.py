from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from app.core.config import settings
from app.services.sms import SmsService
from app.services.sms_queue import (
    SmsQueueService, SmsRequestContext, SmsTransportError, retry_delay,
    sms_request_context, use_sms_context,
)


def test_context_priority_is_scoped_and_restored():
    assert sms_request_context.get() is None
    with use_sms_context(recipient_id=1, priority=100, enqueue_only=True):
        assert sms_request_context.get().recipient_id == 1
        with use_sms_context(SmsRequestContext(priority=0)):
            assert sms_request_context.get().priority == 0
        assert sms_request_context.get().priority == 100
    assert sms_request_context.get() is None


def test_stable_account_key_survives_token_rotation(monkeypatch):
    monkeypatch.setattr(settings, "sms_club_account_key", "shared-account")
    monkeypatch.setattr(settings, "sms_club_token", "first-token")
    initial = SmsQueueService.idempotency_key("recipient:7:sms")
    monkeypatch.setattr(settings, "sms_club_token", "rotated-token")
    assert SmsQueueService.idempotency_key("recipient:7:sms") == initial
    assert "token" not in initial
    monkeypatch.setattr(settings, "sms_club_account_key", "different-account")
    assert SmsQueueService.idempotency_key("recipient:7:sms") != initial


def test_bounded_backoff_preserves_provider_retry_after(monkeypatch):
    monkeypatch.setattr(settings, "sms_queue_retry_base_seconds", 1)
    monkeypatch.setattr(settings, "sms_queue_retry_max_seconds", 60)
    monkeypatch.setattr("app.services.sms_queue.random.uniform", lambda lower, upper: upper)
    assert retry_delay(1) == 1
    assert retry_delay(5) == 16
    assert retry_delay(100) == 60
    assert retry_delay(1, 120) == 120


@pytest.mark.parametrize("operation,payload,kwargs", [
    ("balance", {}, {}),
    ("send", {"phone": [], "message": "hello"}, {}),
    ("send", {"phone": ["1", "2"], "message": "hello"}, {}),
    ("status", {"id_sms": ["1"] * 101}, {}),
    ("send", {"phone": ["1"], "message": "hello", "token": "never-persist"}, {}),
    ("send", {"phone": ["1"], "message": "hello"}, {"context": {"otp_code": "123456"}}),
])
def test_queue_rejects_invalid_or_sensitive_metadata_before_database(operation, payload, kwargs):
    with pytest.raises(ValueError):
        asyncio.run(SmsQueueService().enqueue(operation, payload, **kwargs))


@pytest.mark.parametrize("code,expected,retryable,ambiguous", [
    (429, "rate_limited", True, False),
    (453, "duplicate_suppressed", False, False),
    (401, "authentication", False, False),
    (400, "provider_rejected", False, False),
    (500, "provider_server_error", True, True),
    (503, "provider_server_error", True, True),
    (504, "provider_server_error", True, True),
])
def test_http_rejections_preserve_safe_retry_and_uncertainty_types(monkeypatch, code, expected, retryable, ambiguous):
    def failing(*args, **kwargs):
        raise HTTPError("https://example.test", code, "provider", {"Retry-After": "7"},
                        io.BytesIO(b'{"message":"must not store an echoed OTP 123456"}'))
    monkeypatch.setattr("app.services.sms.request.urlopen", failing)
    with pytest.raises(SmsTransportError) as caught:
        SmsService()._post_json("https://example.test", {}, {})
    assert caught.value.code == expected
    assert caught.value.retryable is retryable
    assert caught.value.ambiguous is ambiguous
    assert "123456" not in caught.value.detail
    if code == 429:
        assert caught.value.retry_after_seconds == 7


def test_network_failure_is_ambiguous_for_send_but_read_can_retry(monkeypatch):
    def failing(*args, **kwargs):
        raise URLError("disconnected")
    monkeypatch.setattr("app.services.sms.request.urlopen", failing)
    with pytest.raises(SmsTransportError) as caught:
        SmsService()._post_json("https://example.test", {}, {})
    assert caught.value.ambiguous
    assert caught.value.retryable


@pytest.mark.parametrize("response", [[], {}, {"success_request": None}, {"success_request": {"add_info": {}}}])
def test_missing_send_id_is_uncertain_even_with_empty_add_info(monkeypatch, response):
    monkeypatch.setattr(settings, "sms_club_token", "fake-test-token")
    service = SmsService()
    monkeypatch.setattr(service, "_post_json", lambda *args: response)
    with pytest.raises(SmsTransportError) as caught:
        asyncio.run(service._execute_queue_job(SimpleNamespace(operation="send", payload={"phone": ["123"], "message": "test"})))
    assert caught.value.ambiguous
    assert not caught.value.retryable


def test_validated_result_drops_unrelated_ids_and_echoed_sensitive_body(monkeypatch):
    monkeypatch.setattr(settings, "sms_club_token", "fake-test-token")
    service = SmsService()
    monkeypatch.setattr(service, "_post_json", lambda *args: {
        "success_request": {"info": {"wrong-id": "999", "correct-id": "123"}},
        "message": "OTP123456", "token": "never-persist",
    })
    result = asyncio.run(service._execute_queue_job(SimpleNamespace(operation="send", payload={"phone": ["123"], "message": "OTP123456"})))
    assert result == {"success_request": {"info": {"correct-id": "123"}}}


def test_otp_uses_same_queue_at_highest_priority_and_never_logs_code(monkeypatch, caplog):
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "fake-test-token")
    recorded = []

    class FakeQueue:
        async def request(self, operation, payload, **kwargs):
            recorded.append((operation, payload, kwargs))
            return {"success_request": {"info": {"42": payload["phone"][0]}}}

    asyncio.run(SmsService(queue=FakeQueue()).send_otp_code("+123", "765432"))
    assert recorded[0][0] == "send"
    assert recorded[0][2]["priority"] == 0
    assert recorded[0][2]["context"].enqueue_only is False
    assert recorded[0][2]["expires_at"] < datetime.now(UTC) + timedelta(minutes=settings.otp_code_ttl_minutes + 1)
    assert "765432" not in caplog.text


def test_retry_after_parses_date_or_invalid_value():
    assert SmsService._retry_after("-1") == 0
    assert SmsService._retry_after("invalid") is None
    assert SmsService._retry_after("inf") is None
    assert SmsService._retry_after("nan") is None
    assert SmsService._retry_after("Wed, 01 Jan 2020 00:00:00 GMT") == 0
