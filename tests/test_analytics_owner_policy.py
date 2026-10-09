"""HTTP contracts for bounded recovery dates and private anonymous telemetry."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import booking_recovery as routes
from app.core.config import settings
from app.core.database import get_db_session
from app.core.rate_limit import SlidingWindowRateLimiter, booking_funnel_rate_limiter
from app.dependencies.auth import get_current_admin_user
from app.schemas.booking_recovery import BookingRecoverySummary
from app.services.booking_recovery_analytics import BookingRecoveryAnalyticsService


@pytest.fixture
def http_client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.public_router)
    app.include_router(routes.backoffice_router)
    app.dependency_overrides[get_db_session] = lambda: object()
    # All existing authenticated admins, including non-superusers, retain access.
    app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=17, is_superuser=False)
    monkeypatch.setattr(routes, "booking_recovery_rate_limiter", SlidingWindowRateLimiter())
    return TestClient(app)


@pytest.mark.parametrize("extra_days,expected", [(365, 200), (366, 422), (-1, 422)])
def test_recovery_http_enforces_inclusive_366_calendar_days(http_client, monkeypatch, extra_days, expected):
    start = date(2024, 1, 1)

    async def summary(_session, *, date_from, date_to):
        BookingRecoveryAnalyticsService.period_bounds(date_from, date_to)
        zero_fields = {
            name: 0 for name, field in BookingRecoverySummary.model_fields.items()
            if field.annotation is int
        }
        return BookingRecoverySummary(
            date_from=date_from, date_to=date_to, **zero_fields,
            alternative_recovery_rate_percent=None, alternative_selection_rate_percent=None,
            alternative_cohort_selection_rate_percent=None, alternative_cohort_completion_rate_percent=None,
            alternative_engagement_collection_started_at=None,
            average_cancellation_to_refill_seconds=None,
            alternative_strict_conversion={
                "requested_sessions": 0, "viewed_sessions": 0, "selected_sessions": 0,
                "completed_sessions": 0, "selection_rate_percent": None, "completion_rate_percent": None,
                "follow_up_observed_until": datetime.now(ZoneInfo("Europe/Kyiv")),
            },
        )

    monkeypatch.setattr(routes.booking_recovery_analytics_service, "summary", summary)
    response = http_client.get("/booking-recovery/summary", params={
        "date_from": start.isoformat(), "date_to": (start + timedelta(days=extra_days)).isoformat(),
    })
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["timezone"] == "Europe/Kyiv"
        assert response.json()["alternative_strict_conversion"]["follow_up_window"] == "observed_until_summary_at"


def test_recovery_http_rate_limit_returns_429_and_preserves_anonymous_event(http_client, monkeypatch):
    recorded = []

    async def record(_session, **kwargs):
        recorded.append(kwargs)
        return True

    monkeypatch.setattr(routes.booking_recovery_analytics_service, "record", record)
    monkeypatch.setattr(settings, "booking_funnel_event_rate_limit", 2)
    payload = {
        "event_id": "evt-private-1234", "anonymous_session_id": "session-private-123456789",
        "event_type": "waitlist_opened",
    }
    assert http_client.post("/booking-recovery/events", json=payload).status_code == 200
    assert http_client.post("/booking-recovery/events", json=payload).status_code == 200
    response = http_client.post("/booking-recovery/events", json=payload)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert len(recorded) == 2
    assert recorded[0]["anonymous_session_id"] == payload["anonymous_session_id"]
    # Rate storage retains only HMAC digests; limiter capacity is separate from funnel telemetry.
    assert routes.booking_recovery_rate_limiter is not booking_funnel_rate_limiter
    assert all(len(key) == 64 and "private" not in key for key in routes.booking_recovery_rate_limiter._entries)
    other_payload = {**payload, "anonymous_session_id": "other-session-123456789"}
    assert http_client.post("/booking-recovery/events", json=other_payload).status_code == 200


def test_recovery_http_rejects_unrepresentable_next_day_before_query(http_client):
    response = http_client.get("/booking-recovery/summary", params={
        "date_from": "9999-12-31", "date_to": "9999-12-31",
    })
    assert response.status_code == 422
