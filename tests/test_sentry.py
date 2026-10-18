"""Sentry stays off without a DSN, and nothing private survives the event hooks."""
from __future__ import annotations

import logging

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app.core import sentry as sentry_module
from app.core.logging import request_id_var
from app.utils.send_booking_sms_reminders import CRON_MONITOR_SLUG, cron_monitor_config

FAKE_DSN = "https://public@o0.ingest.sentry.invalid/1"
# Kept away from the failing handler: Sentry attaches nearby source lines as context.
PHONE = "+380" + "671234567"
NAME = "Ol" + "ena"
JWT = "jwt-" + "secret"
REPEAT_TOKEN = "rb-" + "secret"
COOKIE = "c-" + "secret"
QUERY = "token=" + "secret"
PATH_TOKEN = "path" + "token42"


class CaptureTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") in {"event", "transaction", "check_in"}:
                self.events.append(item.payload.json)


@pytest.fixture(autouse=True)
def isolated_sentry(monkeypatch):
    monkeypatch.setattr(sentry_module.settings, "sentry_dsn", None)
    monkeypatch.setattr(sentry_module.settings, "sentry_release", None)
    monkeypatch.setattr(sentry_module.settings, "sentry_traces_sample_rate", 0.05)
    monkeypatch.setattr(sentry_module.settings, "sentry_event_throttle_seconds", 600)
    sentry_module._last_sent.clear()
    yield
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)
    sentry_module._last_sent.clear()


def start_capturing(**overrides) -> CaptureTransport:
    transport = CaptureTransport()
    options = {**sentry_module.sentry_options(), "dsn": FAKE_DSN, "transport": transport, **overrides}
    sentry_sdk.init(**options)
    return transport


@pytest.mark.parametrize("dsn", [None, "", "   "])
def test_init_is_noop_without_dsn(monkeypatch, dsn):
    calls = []
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sentry_module.settings, "sentry_dsn", dsn)

    assert sentry_module.init_sentry("api") is False
    assert calls == []


def test_init_uses_private_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sentry_module.settings, "sentry_dsn", FAKE_DSN)
    monkeypatch.setattr(sentry_module.settings, "sentry_release", "abc123")
    monkeypatch.setattr(sentry_module.settings, "app_env", "production")

    assert sentry_module.init_sentry("api") is True
    (options,) = calls
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["environment"] == "production"
    assert options["release"] == "abc123"


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        (
            "(psycopg2.errors.UniqueViolation) duplicate key\nDETAIL:  Key (phone)=(+380671234567) already exists.\n"
            "[SQL: INSERT INTO customers (phone) VALUES (%(phone)s)]\n[parameters: {'phone': '+380671234567', 'name': 'Olena'}]\n"
            "(Background on this error at: https://sqlalche.me/e/20/gkpj)",
            ["+380671234567", "Olena"],
        ),
        ("POST https://api.telegram.org/bot123456:AAH-secret_Token/sendMessage failed", ["AAH-secret_Token"]),
        ("Authorization: Bearer abc.def-ghi", ["abc.def-ghi"]),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl rejected", ["eyJhbGciOiJIUzI1NiJ9"]),
        ("GET https://api.example.com/v1/x?apiKey=secret#frag", ["apiKey", "secret", "frag"]),
        ("send to olena.k+test@gmail.com failed", ["olena.k+test@gmail.com"]),
        ("SMS to 0671234567 and +380501112233 and +447700900123", ["0671234567", "380501112233", "447700900123"]),
        (
            "+38 (067) 123-45-67, 067 123 45 67, 067-123-45-67, +380 67 123 45 67, (067)1234567, tel:+38 067 123 4567",
            ["123-45-67", "123 45 67", "1234567", "123 4567"],
        ),
        ("connect postgresql+asyncpg://app:db-pass@postgres:5432/x failed", ["db-pass"]),
        ("brevo api-key: xkeysib-123 rejected, password=hunter2", ["xkeysib-123", "hunter2"]),
        ("OTP code 123456 for user", ["123456"]),
    ],
)
def test_scrub_text_removes_private_values(raw, leaked):
    scrubbed = sentry_module.scrub_text(raw)

    for value in leaked:
        assert value not in scrubbed


def test_scrub_text_keeps_dates_ids_and_sql_shape():
    raw = (
        "booking 12345 at 2026-10-18 14:30 failed: [SQL: SELECT * FROM bookings WHERE id = %(id)s] "
        "etag abcdef0123456789abcdef uuid 3f2c0123456789ab-cdef status code 500"
    )

    assert sentry_module.scrub_text(raw) == raw


def test_before_send_scrubs_request_and_tags_request_id():
    event = {
        "user": {"ip_address": "203.0.113.5"},
        "request": {
            "url": "https://api.soulcuts.com.ua/api/v1/public/reviews/request?token=abc#x",
            "query_string": "token=abc",
            "cookies": {"sc_customer_activity": "secret"},
            "data": {"phone": "+380671234567"},
            "env": {"REMOTE_ADDR": "203.0.113.5"},
            "headers": {
                "Authorization": "Bearer jwt",
                "Cookie": "sc_customer_activity=secret",
                "X-Review-Token": "r",
                "X-Repeat-Booking-Token": "rb",
                "X-Customer-Activity-Token": "ca",
                "X-Telegram-Bot-Api-Secret-Token": "tg",
                "X-Forwarded-For": "203.0.113.5",
                "User-Agent": "pytest",
                "X-Request-Id": "rid-1",
            },
        },
        "transaction": "/api/v1/public/reviews/request",
        "transaction_info": {"source": "route"},
        "logentry": {"message": "Login for %s failed", "params": ["+380671234567"], "formatted": "Login for +380671234567 failed"},
    }
    token = request_id_var.set("rid-1")
    try:
        result = sentry_module.before_send(event, {})
    finally:
        request_id_var.reset(token)

    assert result is event
    assert "user" not in event
    request = event["request"]
    assert request["url"] == "https://api.soulcuts.com.ua/api/v1/public/reviews/request"
    assert {"query_string", "cookies", "data", "env"}.isdisjoint(request)
    assert request["headers"] == {"User-Agent": "pytest"}
    assert event["logentry"] == {"message": "Login for %s failed", "formatted": "Login for [phone] failed"}
    assert event["tags"]["request_id"] == "rid-1"


def test_unmatched_request_path_and_name_are_hidden():
    event = {
        "transaction": "http://api.test/blog/subscriptions/SECRET",
        "transaction_info": {"source": "url"},
        "request": {"url": "http://api.test/blog/subscriptions/SECRET"},
    }

    sentry_module.before_send(event, {})

    assert event["request"]["url"] == "http://api.test/[Filtered]"
    assert event["transaction"] == "unmatched-route"


@pytest.mark.parametrize("request_id", ["x" * 65, "has space", "+380671234567"])
def test_untrusted_request_id_is_not_tagged(request_id):
    event = {"logentry": {"message": "boom"}}
    token = request_id_var.set(request_id)
    try:
        sentry_module.before_send(event, {})
    finally:
        request_id_var.reset(token)

    assert "request_id" not in event.get("tags", {})


def test_check_ins_bypass_throttle():
    for status in ("in_progress", "ok"):
        event = {"type": "check_in", "monitor_slug": CRON_MONITOR_SLUG, "status": status}
        assert sentry_module.before_send(event, {}) is event


def test_before_send_throttles_identical_events(monkeypatch):
    clock = iter([1000.0, 1100.0, 1700.0])
    monkeypatch.setattr(sentry_module.time, "monotonic", lambda: next(clock))

    def worker_event():
        return {"logger": "app.services.sms_queue_scheduler", "logentry": {"message": "SMS queue worker iteration failed"}}

    assert sentry_module.before_send(worker_event(), {}) is not None
    assert sentry_module.before_send(worker_event(), {}) is None
    assert sentry_module.before_send(worker_event(), {}) is not None


def test_throttle_keys_exceptions_by_type_and_location(monkeypatch):
    monkeypatch.setattr(sentry_module.time, "monotonic", lambda: 1000.0)

    def exception_event(exc_type, function):
        frames = [
            {"module": "app.services.booking", "function": function, "in_app": True},
            {"module": "httpx._models", "function": "raise_for_status", "in_app": False},
        ]
        return {"exception": {"values": [{"type": exc_type, "value": "x", "stacktrace": {"frames": frames}}]}}

    assert sentry_module.before_send(exception_event("KeyError", "create"), {}) is not None
    assert sentry_module.before_send(exception_event("KeyError", "cancel"), {}) is not None
    assert sentry_module.before_send(exception_event("ValueError", "create"), {}) is not None
    assert sentry_module.before_send(exception_event("KeyError", "create"), {}) is None


def test_throttle_can_be_disabled(monkeypatch):
    monkeypatch.setattr(sentry_module.settings, "sentry_event_throttle_seconds", 0)
    event = {"logentry": {"message": "boom"}}

    assert sentry_module.before_send(dict(event), {}) is not None
    assert sentry_module.before_send(dict(event), {}) is not None


def test_transaction_spans_and_breadcrumbs_are_scrubbed():
    event = {
        "spans": [
            {
                "description": "POST https://api.telegram.org/bot1:ABC/sendMessage",
                "data": {"url": "https://api.telegram.org/bot1:ABC/sendMessage", "http.query": "chat_id=1"},
            }
        ],
        "breadcrumbs": {
            "values": [
                {
                    "message": "OTP for 0671234567",
                    "data": {"url": "https://x.test/a?key=1", "http.query": "text=Hi+Olena", "phone": "067 123 45 67", "name": "Olena"},
                }
            ]
        },
    }

    result = sentry_module.before_send_transaction(event, {})

    span = result["spans"][0]
    assert "ABC" not in span["description"]
    assert span["data"] == {"url": "https://api.telegram.org/bot[Filtered]/sendMessage"}
    crumb = result["breadcrumbs"]["values"][0]
    assert crumb["message"] == "OTP for [phone]"
    assert crumb["data"] == {"url": "https://x.test/a?[Filtered]", "phone": "[Filtered]", "name": "[Filtered]"}


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"path": "/api/v1/public/health", "method": "GET"}, 0.0),
        ({"path": "/media/products/a.webp", "method": "GET"}, 0.0),
        ({"path": "/api/v1/public/bookings", "method": "OPTIONS"}, 0.0),
        ({"path": "/api/v1/public/bookings", "method": "POST"}, 0.05),
    ],
)
def test_traces_sampler_skips_noise(scope, expected):
    assert sentry_module.traces_sampler({"asgi_scope": scope}) == expected


def test_real_sdk_events_carry_no_private_data():
    transport = start_capturing(traces_sampler=None, traces_sample_rate=0)
    app = FastAPI()

    @app.post("/api/v1/public/bookings/{token}")
    async def failing_booking(token: str):
        phone = PHONE  # noqa: F841 - would be captured if local variables leaked
        raise RuntimeError(f"Booking for {PHONE} failed")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/api/v1/public/bookings/{PATH_TOKEN}?{QUERY}",
        json={"phone": PHONE, "name": NAME},
        headers={"Authorization": f"Bearer {JWT}", "X-Repeat-Booking-Token": REPEAT_TOKEN, "Cookie": f"sc_customer_activity={COOKIE}"},
    )
    sentry_sdk.flush()

    assert response.status_code == 500
    errors = [event for event in transport.events if event.get("exception")]
    assert errors, "the unhandled exception must reach Sentry"
    payload = repr(errors)
    for secret in (PHONE, NAME, JWT, REPEAT_TOKEN, COOKIE, QUERY, PATH_TOKEN):
        assert secret not in payload
    assert all("vars" not in frame for event in errors for value in event["exception"]["values"] for frame in value["stacktrace"]["frames"])


def test_real_sdk_logger_exception_is_reported_once_per_window():
    transport = start_capturing()
    logger = logging.getLogger("app.services.campaign_run_scheduler")

    for _ in range(3):
        try:
            raise ConnectionError("database unavailable")
        except ConnectionError:
            logger.exception("Campaign run scheduler iteration failed")
    sentry_sdk.flush()

    errors = [event for event in transport.events if event.get("level") == "error"]
    assert len(errors) == 1
    assert errors[0]["exception"]["values"][-1]["type"] == "ConnectionError"


def test_real_sdk_cron_monitor_reports_start_and_finish():
    from sentry_sdk.crons import monitor

    transport = start_capturing()
    for _ in range(2):
        with monitor(monitor_slug=CRON_MONITOR_SLUG, monitor_config=cron_monitor_config()):
            pass
    sentry_sdk.flush()

    statuses = [event["status"] for event in transport.events if event.get("type") == "check_in"]
    assert statuses == ["in_progress", "ok", "in_progress", "ok"]


def test_cron_monitor_follows_reminder_interval(monkeypatch):
    monkeypatch.setenv("BOOKING_SMS_REMINDER_INTERVAL_SECONDS", "90")
    assert cron_monitor_config()["schedule"] == {"type": "interval", "value": 2, "unit": "minute"}

    monkeypatch.setenv("BOOKING_SMS_REMINDER_INTERVAL_SECONDS", "five minutes")
    assert cron_monitor_config()["schedule"] == {"type": "interval", "value": 5, "unit": "minute"}

    monkeypatch.delenv("BOOKING_SMS_REMINDER_INTERVAL_SECONDS")
    assert cron_monitor_config()["schedule"] == {"type": "interval", "value": 5, "unit": "minute"}
