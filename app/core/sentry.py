"""Sentry error and performance reporting.

Disabled unless SENTRY_DSN is set. Events never carry request bodies, cookies,
auth headers, query strings, local variables or client IPs, and free text is
scrubbed of phones, emails, tokens and SQL parameters before it leaves the
process. Repeated identical events are throttled per process so a broken
background loop cannot exhaust the free-plan quota.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import sentry_sdk
from sentry_sdk.integrations.logging import ignore_logger
from app.core import error_reporting

from app.core.config import settings
from app.core.logging import request_id_var

FILTERED = "[Filtered]"

# Everything else (Authorization, Cookie, review/repeat/activity tokens, the
# Telegram webhook secret, forwarded client IPs) is dropped.
_ALLOWED_HEADERS = frozenset({"accept", "content-length", "content-type", "host", "user-agent"})

_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # SQLAlchemy appends bound values: "[parameters: (...)]".
    (re.compile(r"\[parameters:.*?(?=\n\(Background|\Z)", re.S), f"[parameters: {FILTERED}]"),
    # PostgreSQL constraint details: "Key (phone)=(+380...)".
    (re.compile(r"(Key \([^)]*\)=\()[^)]*(\))"), rf"\g<1>{FILTERED}\g<2>"),
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), f"bot{FILTERED}"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {FILTERED}"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), FILTERED),
    # Credentials in connection URLs: "postgresql://user:password@host".
    (re.compile(r"(\w[\w+.-]*://[^:/\s@]+:)[^@\s]+@"), rf"\g<1>{FILTERED}@"),
    (
        re.compile(r"(?i)\b(api[-_]?key|apikey|token|secret|password|otp)(\s*[:=]\s*)[^\s,;&'\"]+"),
        rf"\g<1>\g<2>{FILTERED}",
    ),
    (re.compile(r"(?i)\b(otp\D{0,12})\d{4,8}\b"), rf"\g<1>{FILTERED}"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    # Ukrainian numbers in any common spelling: +380671234567, +38 (067) 123-45-67, 067 123 45 67.
    (
        re.compile(r"(?<![\w+])(?:\+?38[\s()-]*)?\(?0[\s-]*\d{2}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\w)"),
        "[phone]",
    ),
    (re.compile(r"(?<![\w+])\+\d{10,15}(?!\w)"), "[phone]"),
)

# Values under these keys (logging `extra`, breadcrumb data) are dropped whatever they contain.
_SENSITIVE_KEYS = frozenset(
    {"phone", "body", "name", "first_name", "last_name", "email", "otp", "code", "token", "password", "secret"}
)
_QUERY_KEYS = frozenset({"http.query", "http.fragment", "query", "query_string"})
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")

_throttle_lock = threading.Lock()
_last_sent: dict[str, float] = {}
_THROTTLE_PRUNE_SIZE = 512


def scrub_text(value: str) -> str:
    for pattern, replacement in _TEXT_RULES:
        value = pattern.sub(replacement, value)
    # Outgoing URLs have no trusted route template; even opaque path components
    # can be credentials. Keep the destination, not its path or query.
    return _URL_PATTERN.sub(lambda match: _safe_url(match.group()), value)


def _safe_url(value: str, route: str | None = None) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, route or f"/{FILTERED}", "", ""))
    except ValueError:
        return FILTERED


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            key: FILTERED if str(key).lower() in _SENSITIVE_KEYS else _scrub(item)
            for key, item in value.items()
            if key not in _QUERY_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


def _route_template(event: dict[str, Any]) -> str | None:
    if (event.get("transaction_info") or {}).get("source") == "route" and isinstance(event.get("transaction"), str):
        return event["transaction"]
    return None


def _scrub_request(request: dict[str, Any], route: str | None) -> None:
    for key in ("cookies", "data", "query_string", "env"):
        request.pop(key, None)
    if isinstance(request.get("url"), str):
        # Paths can carry tokens (/blog/subscriptions/{token}); keep only the route template.
        request["url"] = _safe_url(request["url"], route)
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {
            name: _scrub(value) for name, value in headers.items() if name.lower() in _ALLOWED_HEADERS
        }


def _scrub_breadcrumb(crumb: dict[str, Any]) -> dict[str, Any]:
    if isinstance(crumb.get("message"), str):
        crumb["message"] = scrub_text(crumb["message"])
    if isinstance(crumb.get("data"), dict):
        crumb["data"] = _scrub(crumb["data"])
    return crumb


def _scrub_event(event: dict[str, Any]) -> None:
    event.pop("user", None)
    route = _route_template(event)
    if isinstance(event.get("request"), dict):
        _scrub_request(event["request"], route)
    if event.get("request") and route is None and isinstance(event.get("transaction"), str):
        # Unmatched requests are named after the raw URL, which may hold tokens.
        event["transaction"] = "unmatched-route"

    for exception in (event.get("exception") or {}).get("values") or []:
        if isinstance(exception.get("value"), str):
            exception["value"] = scrub_text(exception["value"])

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        logentry.pop("params", None)
        for key in ("message", "formatted"):
            if isinstance(logentry.get(key), str):
                logentry[key] = scrub_text(logentry[key])
    if isinstance(event.get("message"), str):
        event["message"] = scrub_text(event["message"])

    if isinstance(event.get("extra"), dict):
        event["extra"] = _scrub(event["extra"])
    if isinstance(event.get("contexts"), dict):
        event["contexts"] = _scrub(event["contexts"])

    breadcrumbs = event.get("breadcrumbs")
    crumbs = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs
    for crumb in crumbs or []:
        if isinstance(crumb, dict):
            _scrub_breadcrumb(crumb)

    for span in event.get("spans") or []:
        if isinstance(span.get("description"), str):
            span["description"] = scrub_text(span["description"])
        if isinstance(span.get("data"), dict):
            span["data"] = _scrub(span["data"])


def _tag_request_id(event: dict[str, Any]) -> None:
    # Custom client IDs may be phone numbers or credentials. Only canonical UUIDs
    # (the format generated by our middleware) are suitable correlation metadata.
    request_id = request_id_var.get()
    try:
        is_uuid = str(UUID(request_id)) == request_id
    except (ValueError, AttributeError, TypeError):
        is_uuid = False
    if is_uuid:
        tags = event.get("tags")
        if not isinstance(tags, dict):
            tags = {}
            event["tags"] = tags
        tags["request_id"] = request_id


def _throttle_key(event: dict[str, Any]) -> str:
    exceptions = (event.get("exception") or {}).get("values") or []
    if exceptions:
        last = exceptions[-1]
        frames = (last.get("stacktrace") or {}).get("frames") or [{}]
        # The deepest app frame tells shared helpers (httpx raise_for_status) apart by call site.
        frame = next((frame for frame in reversed(frames) if frame.get("in_app")), frames[-1])
        return f"exc:{last.get('type')}:{frame.get('module')}:{frame.get('function')}"
    logentry = event.get("logentry") or {}
    return f"msg:{event.get('logger')}:{logentry.get('message') or event.get('message')}"


def _should_throttle(event: dict[str, Any]) -> bool:
    window = settings.sentry_event_throttle_seconds
    if window <= 0:
        return False
    key = _throttle_key(event)
    now = time.monotonic()
    with _throttle_lock:
        last = _last_sent.get(key)
        if last is not None and now - last < window:
            return True
        _last_sent[key] = now
        if len(_last_sent) > _THROTTLE_PRUNE_SIZE:
            for stale_key, sent_at in list(_last_sent.items()):
                if now - sent_at >= window:
                    del _last_sent[stale_key]
    return False


def _private_rewards(value: Any) -> bool:
    """Never export this internal accounting domain, even as SQL breadcrumbs."""
    if isinstance(value, str):
        return any(marker in value for marker in ('/backoffice/master-rewards',
            'master_reward_', 'app.services.master_rewards', 'app.schemas.master_rewards',
            'app.api.v1.routes.master_rewards', 'app.models.master_rewards'))
    if isinstance(value, dict):
        return any(str(key) in {'rate_bps', 'reward_minor', 'expected_total_minor'}
            or _private_rewards(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_private_rewards(item) for item in value)
    return False


def before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    if _private_rewards(event):
        return None
    if event.get("logger") == "backend.technical":
        # Manual financial incidents already own durable admission and strict PII filtering.
        safe = error_reporting.before_send(event, _hint)
        if safe is not None:
            if settings.sentry_release:
                safe['release'] = scrub_text(settings.sentry_release)
            component = (event.get('tags') or {}).get('component')
            if component in {'api', 'booking-sms-reminders'}:
                safe['tags']['component'] = component
            _tag_request_id(safe)
        return safe
    record = _hint.get("log_record")
    info = _hint.get("exc_info") or getattr(record, "exc_info", None) or (None, None, None)
    exception = info[1]
    if getattr(exception, "_technical_reported", False):
        return None
    # Cron check-ins also pass through here; they carry no user data and must never be throttled.
    if event.get("type") is not None:
        return event
    if _should_throttle(event):
        return None
    _scrub_event(event)
    _tag_request_id(event)
    return event


def before_send_transaction(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    if _private_rewards(event):
        return None
    _scrub_event(event)
    _tag_request_id(event)
    return event


def before_breadcrumb(crumb: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    if _private_rewards(crumb):
        return None
    return _scrub_breadcrumb(crumb)


def traces_sampler(sampling_context: dict[str, Any]) -> float:
    scope = sampling_context.get("asgi_scope") or {}
    path = scope.get("path") or ""
    if _private_rewards(path):
        return 0.0
    if scope.get("method") == "OPTIONS":
        return 0.0
    if path.endswith("/public/health") or path.startswith(settings.upload_url_prefix):
        return 0.0
    return settings.sentry_traces_sample_rate


def sentry_options() -> dict[str, Any]:
    return {
        "dsn": settings.sentry_dsn,
        "environment": settings.app_env,
        "release": settings.sentry_release or None,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "traces_sampler": traces_sampler,
        "before_send": before_send,
        "before_send_transaction": before_send_transaction,
        "before_breadcrumb": before_breadcrumb,
    }


def init_sentry(component: str) -> bool:
    """Initialise Sentry for one process; returns False when no DSN is configured."""
    if not (settings.sentry_dsn or "").strip():
        return False
    error_reporting.initialize(settings)
    # Access log lines carry client IPs and raw query strings; Caddy metrics cover traffic.
    ignore_logger("uvicorn.access")
    sentry_sdk.init(**sentry_options())
    sentry_sdk.get_global_scope().set_tag("component", component)
    return True
