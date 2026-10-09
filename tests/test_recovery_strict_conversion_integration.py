"""Chronological same-offer recovery conversion without altering legacy metrics."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.v1.routes import booking_recovery as routes
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.booking_recovery import BookingRecoveryEventType as Event
from app.services.booking_recovery_analytics import BookingRecoveryAnalyticsService
from test_segments_integration import anyio_backend, database

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.mark.anyio
@pytest.mark.parametrize("zone", ["UTC", "Europe/Kyiv"])
async def test_strict_same_offer_chronology_cohort_and_http_legacy_contract(database, zone):
    service = BookingRecoveryAnalyticsService()
    at = datetime(2026, 10, 2, 12, tzinfo=KYIV)
    observation = datetime(2026, 10, 9, 12, tzinfo=KYIV)
    class ObservedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return observation.astimezone(tz)
    async with database() as session:
        async def event(kind, name, minute=0, *, offer="offer-a-123456789", context=None, anonymous=None, observed=None):
            return await service.record(
                session, event_type=kind, event_key=f"{name}:{kind.value}:{minute}:{offer}",
                anonymous_session_id=anonymous or f"session-{name}-123456789",
                search_context_id=context or f"context-{name}-123456789",
                offer_id=offer, occurred_at=observed or at + timedelta(minutes=minute),
            )

        names = ("good", "late", "offer-mismatch", "context-mismatch", "reversed", "equal", "early-completion", "future")
        for name in names:
            await event(Event.alternatives_requested, name)
            await event(Event.alternative_slot_viewed, name, 1,
                        context="different-context-123456789" if name == "context-mismatch" else None)
            await event(Event.alternative_slot_selected, name, 0 if name == "reversed" else 1 if name == "equal" else 2,
                        offer="offer-b-123456789" if name == "offer-mismatch" else "offer-a-123456789")
            completed_at = (
                at + timedelta(days=6) if name == "late" else
                observation + timedelta(days=2) if name == "future" else
                at + timedelta(minutes=1 if name == "early-completion" else 3)
            )
            await event(Event.booking_completed_after_alternative, name, 3, observed=completed_at)
        # Multiple offers and contexts remain one distinct converted session.
        for kind, minute in ((Event.alternative_slot_viewed, 4), (Event.alternative_slot_selected, 5),
                             (Event.booking_completed_after_alternative, 6)):
            await event(kind, "good", minute, offer="offer-b-123456789")
        assert not await event(Event.alternative_slot_viewed, "good", 1)
        # Strict > excludes a request/view tie; boundaries are Kyiv start inclusive/end exclusive.
        for name, request_at in (("start", datetime(2026, 10, 1, tzinfo=KYIV)),
                                 ("before", datetime(2026, 10, 1, tzinfo=KYIV) - timedelta(microseconds=1)),
                                 ("end", datetime(2026, 10, 5, tzinfo=KYIV))):
            for kind, minute in ((Event.alternatives_requested, 0), (Event.alternative_slot_viewed, 1),
                                 (Event.alternative_slot_selected, 2), (Event.booking_completed_after_alternative, 3)):
                await event(kind, name, minute, observed=request_at + timedelta(minutes=minute))
        # Missing attribution is retained in raw period aggregates, excluded only from strict metrics.
        for name, anonymous, context in (("null-context", "null-session-context-123456789", None),
                                          ("null-session", None, "null-context-session-123456789")):
            for kind, minute in ((Event.alternatives_requested, 0), (Event.alternative_slot_viewed, 1),
                                 (Event.alternative_slot_selected, 2), (Event.booking_completed_after_alternative, 3)):
                await service.record(session, event_type=kind, event_key=f"{name}:{kind.value}",
                                     anonymous_session_id=anonymous, search_context_id=context,
                                     offer_id="offer-a-123456789", occurred_at=at + timedelta(minutes=minute))
        await session.commit()
        await session.execute(text("SELECT set_config('TimeZone', :zone, true)"), {"zone": zone})

        app = FastAPI()
        app.include_router(routes.backoffice_router)
        async def same_session():
            yield session
        app.dependency_overrides[get_db_session] = same_session
        app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=1, is_superuser=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with patch("app.services.booking_recovery_analytics.datetime", ObservedDatetime):
                response = await client.get("/booking-recovery/summary", params={"date_from": "2026-10-01", "date_to": "2026-10-04"})
        assert response.status_code == 200
        payload = response.json()
        metric = payload["alternative_strict_conversion"]
        assert tuple(metric[key] for key in ("requested_sessions", "viewed_sessions", "selected_sessions", "completed_sessions")) == (9, 8, 5, 3)
        assert Decimal(metric["selection_rate_percent"]) == Decimal("62.50")
        assert Decimal(metric["completion_rate_percent"]) == Decimal("33.33")
        assert metric["follow_up_window"] == "observed_until_summary_at"
        assert datetime.fromisoformat(metric["follow_up_observed_until"]) == observation
        assert payload["alternatives_requested"] == 11
        # Existing cohort allows late completions and completion without selection/view matching.
        assert payload["alternative_cohort_requested_sessions"] == 9
        assert payload["alternative_cohort_completed_sessions"] == 9
        assert Decimal(payload["alternative_cohort_completion_rate_percent"]) == Decimal("100.00")
        assert "phone" not in response.text and "session-good" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
@pytest.mark.parametrize("zone", ["UTC", "Europe/Kyiv"])
async def test_recovery_calendar_day_boundaries_across_dst(database, day, zone):
    # Freeze observation cutoff so future calendar fixtures remain deterministic.
    service = BookingRecoveryAnalyticsService()
    start, end = service.period_bounds(day, day)
    assert start.utcoffset() != end.utcoffset()
    class ObservedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 1, tzinfo=KYIV).astimezone(tz)
    async with database() as session:
        for key, observed in (("before", start - timedelta(microseconds=1)), ("start", start),
                              ("last", end - timedelta(microseconds=1)), ("end", end)):
            await service.record(session, event_type=Event.alternatives_requested, event_key=key,
                                 anonymous_session_id=f"session-{key}-123456789",
                                 search_context_id=f"context-{key}-123456789", occurred_at=observed)
        await session.commit()
        await session.execute(text("SELECT set_config('TimeZone', :zone, true)"), {"zone": zone})
        with patch("app.services.booking_recovery_analytics.datetime", ObservedDatetime):
            result = await service.summary(session, date_from=day, date_to=day)
        assert result.alternatives_requested == 2
        assert result.alternative_strict_conversion.requested_sessions == 2
        assert result.alternative_strict_conversion.viewed_sessions == 0
        assert result.alternative_strict_conversion.selection_rate_percent is None
        assert result.alternative_strict_conversion.completion_rate_percent == Decimal("0.00")
