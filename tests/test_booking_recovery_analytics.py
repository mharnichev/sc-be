from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.api.v1.routes import booking_recovery as booking_recovery_routes
from app.api.v1.routes import booking_alternatives as booking_alternatives_routes
from app.api.v1.routes import bookings as booking_routes
from app.models.booking_recovery import BookingRecoveryEventType
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.schemas.booking import PublicBookingCreate
from app.schemas.booking_recovery import PublicBookingRecoveryEventCreate
from app.services.booking_recovery_analytics import BookingRecoveryAnalyticsService


class AnalyticsResult:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar

    def all(self):
        return self.rows

    def one(self):
        return self.rows[0]


class AnalyticsSession:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, _statement):
        return self.results.pop(0)


def test_recovery_period_uses_kyiv_calendar_boundaries_across_dst() -> None:
    start, end = BookingRecoveryAnalyticsService.period_bounds(date(2026, 3, 29), date(2026, 3, 29))
    assert start.tzinfo.key == "Europe/Kyiv"
    assert end.tzinfo.key == "Europe/Kyiv"
    assert start.date() == date(2026, 3, 29)
    assert end.date() == date(2026, 3, 30)
    assert start.utcoffset() != end.utcoffset()


def test_recovery_identifier_hash_does_not_persist_raw_session_value() -> None:
    raw = "anonymous-session-123456789"
    hashed = BookingRecoveryAnalyticsService.hash_identifier("booking_recovery_session", raw)
    assert raw not in hashed
    assert len(hashed) == 64


@pytest.mark.anyio
async def test_recovery_summary_exposes_operational_counters_without_personal_data() -> None:
    rows = [
        (BookingRecoveryEventType.alternatives_requested.value, 4, 0),
        (BookingRecoveryEventType.alternatives_returned.value, 3, 8),
        (BookingRecoveryEventType.alternative_slot_viewed.value, 4, 0),
        (BookingRecoveryEventType.alternative_slot_selected.value, 2, 0),
        (BookingRecoveryEventType.booking_completed_after_alternative.value, 1, 0),
        (BookingRecoveryEventType.waitlist_submitted.value, 5, 0),
        (BookingRecoveryEventType.waitlist_offer_sent.value, 4, 0),
        (BookingRecoveryEventType.waitlist_offer_delivered.value, 3, 0),
        (BookingRecoveryEventType.waitlist_offer_claimed.value, 2, 0),
        (BookingRecoveryEventType.waitlist_offer_expired.value, 1, 0),
        (BookingRecoveryEventType.booking_completed_after_waitlist_offer.value, 2, 180),
    ]
    summary = await BookingRecoveryAnalyticsService().summary(
        AnalyticsSession(
            [
                AnalyticsResult(scalar=6),
                AnalyticsResult(rows=rows),
                AnalyticsResult(scalar=datetime(2026, 8, 1, 9, 30)),
                AnalyticsResult(rows=[(2, 90)]),
                AnalyticsResult(rows=[(4, 3, 2, 1)]),
                AnalyticsResult(rows=[(4, 3, 1, 1)]),
            ]
        ),
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 5),
    )

    assert summary.no_slot_sessions == 6
    assert summary.alternative_recovery_rate_percent == 25
    assert summary.alternative_slots_returned == 8
    assert summary.alternative_slots_viewed == 4
    assert summary.alternative_selection_rate_percent == 50
    assert summary.alternative_cohort_requested_sessions == 4
    assert summary.alternative_cohort_viewed_sessions == 3
    assert summary.alternative_cohort_selected_sessions == 2
    assert summary.alternative_cohort_completed_sessions == 1
    assert summary.alternative_cohort_selection_rate_percent == Decimal("66.67")
    assert summary.alternative_cohort_completion_rate_percent == 25
    assert summary.alternative_strict_conversion.selected_sessions == 1
    assert summary.alternative_strict_conversion.selection_rate_percent == Decimal("33.33")
    assert summary.alternative_strict_conversion.completion_rate_percent == 25
    assert summary.alternative_engagement_collection_started_at == datetime(2026, 8, 1, 9, 30)
    assert summary.waitlist_requests == 5
    assert summary.offers_claimed == 2
    assert summary.cancelled_slots_refilled == 2
    assert summary.average_cancellation_to_refill_seconds == 90
    assert "phone" not in summary.model_dump()


def test_viewed_event_requires_opaque_context_and_offer() -> None:
    common = {
        "event_id": "evt-01HZY7QX6FD5",
        "anonymous_session_id": "anonymous-session-123456789",
        "event_type": "alternative_slot_viewed",
    }
    with pytest.raises(ValidationError):
        PublicBookingRecoveryEventCreate(**common)
    payload = PublicBookingRecoveryEventCreate(
        **common,
        search_context_id="context-01HZY7QX6FD5",
        offer_id="offer-01HZY7QX6FD5",
    )
    assert payload.search_context_id == "context-01HZY7QX6FD5"


def test_booking_context_and_offer_are_a_compatible_pair() -> None:
    base = {
        "master_id": 1,
        "service_id": 1,
        "customer_name": "Customer",
        "customer_phone": "+380501112233",
        "start_at": datetime(2026, 10, 7, 10, 0),
    }
    with pytest.raises(ValidationError):
        PublicBookingCreate(
            **base,
            recovery_search_context_id="context-01HZY7QX6FD5",
        )
    payload = PublicBookingCreate(
        **base,
        recovery_source="alternative",
        funnel_session_id="anonymous-session-123456789",
        recovery_search_context_id="context-01HZY7QX6FD5",
        recovery_offer_id="offer-01HZY7QX6FD5",
    )
    assert payload.recovery_offer_id == "offer-01HZY7QX6FD5"


@pytest.mark.anyio
async def test_client_view_deduplicates_by_semantic_offer_not_retry_event_id(monkeypatch) -> None:
    calls = []

    async def record(_session, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(booking_recovery_routes.booking_recovery_analytics_service, "record", record)
    payload = PublicBookingRecoveryEventCreate(
        event_id="evt-01HZY7QX6FD5",
        anonymous_session_id="anonymous-session-123456789",
        event_type="alternative_slot_viewed",
        search_context_id="context-01HZY7QX6FD5",
        offer_id="offer-01HZY7QX6FD5",
    )
    result = await booking_recovery_routes.record_booking_recovery_event(payload, Request({"type": "http"}), session=object())
    assert result.status == "recorded"
    assert calls[0]["event_key"] == (
        "client:alternative_slot_viewed:anonymous-session-123456789:"
        "context-01HZY7QX6FD5:offer-01HZY7QX6FD5"
    )


@pytest.mark.anyio
async def test_client_event_analytics_failure_is_reported_without_raising(monkeypatch, caplog) -> None:
    class Session:
        rolled_back = False

        async def rollback(self):
            self.rolled_back = True

    async def fail_record(*_args, **_kwargs):
        raise RuntimeError("analytics unavailable")

    session = Session()
    monkeypatch.setattr(booking_recovery_routes.booking_recovery_analytics_service, "record", fail_record)
    result = await booking_recovery_routes.record_booking_recovery_event(
        PublicBookingRecoveryEventCreate(
            event_id="evt-01HZY7QX6FD5",
            anonymous_session_id="anonymous-session-123456789",
            event_type="alternative_slot_selected",
        ),
        request=Request({"type": "http"}),
        session=session,
    )
    assert result.status == "unavailable"
    assert session.rolled_back is True
    assert "Booking recovery client analytics failed" in caplog.text


@pytest.mark.anyio
async def test_completion_analytics_failure_cannot_break_committed_booking(monkeypatch, caplog) -> None:
    class AnalyticsSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fail_record(*_args, **_kwargs):
        raise RuntimeError("analytics unavailable")

    monkeypatch.setattr(booking_routes, "AsyncSessionLocal", AnalyticsSession)
    monkeypatch.setattr(booking_routes.booking_recovery_analytics_service, "record", fail_record)
    await booking_routes.record_alternative_booking_completion(
        payload=PublicBookingCreate(
            master_id=1, service_id=1, customer_name="Customer", customer_phone="+380501112233",
            start_at=datetime(2026, 10, 7, 10, 0), recovery_source="alternative",
            funnel_session_id="anonymous-session-123456789",
        ),
        booking=SimpleNamespace(id=42, service_id=1),
    )
    assert "Booking recovery completion analytics failed" in caplog.text


@pytest.mark.anyio
async def test_alternatives_analytics_failure_cannot_break_search(monkeypatch, caplog) -> None:
    response = SimpleNamespace(search_context_id="a" * 32, same_master=[object()], other_masters=[])

    async def find(*_args, **_kwargs):
        return response

    async def fail_record(*_args, **_kwargs):
        raise RuntimeError("analytics unavailable")

    class Session:
        rolled_back = False

        async def rollback(self):
            self.rolled_back = True

    session = Session()
    monkeypatch.setattr(booking_alternatives_routes.service, "find_page", find)
    monkeypatch.setattr(booking_alternatives_routes.booking_recovery_analytics_service, "record", fail_record)
    result = await booking_alternatives_routes.booking_alternatives(
        SimpleNamespace(
            funnel_session_id="anonymous-session-123456789",
            master_id=1,
            service_ids=[1],
            desired_date=date(2026, 10, 7),
            duration_minutes=60,
            offset=0,
            page_size=9,
        ),
        session=session,
    )
    assert result is response
    assert session.rolled_back is True
    assert "Booking alternatives request analytics failed" in caplog.text


@pytest.mark.anyio
async def test_paginated_alternative_telemetry_deduplicates_retries_and_counts_each_page(monkeypatch) -> None:
    events = []

    class Session:
        commits = 0

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            raise AssertionError("analytics should not roll back")

    class AlternativesService:
        def _search_context_id(self, _payload, _duration):
            return "context-01HZY7QX6FD5"

        async def find_page(self, _session, payload):
            assert payload.page_size == 3
            count = 3 if payload.offset == 0 else 2
            return SimpleNamespace(
                search_context_id="context-01HZY7QX6FD5",
                same_master=[object()] * count,
                other_masters=[],
            )

    async def record(_session, **kwargs):
        events.append(kwargs)
        return True

    monkeypatch.setattr(booking_alternatives_routes, "service", AlternativesService())
    monkeypatch.setattr(booking_alternatives_routes.booking_recovery_analytics_service, "record", record)
    session = Session()
    common = {
        "master_id": 1,
        "service_ids": [1],
        "desired_date": date(2026, 10, 7),
        "duration_minutes": 60,
        "funnel_session_id": "anonymous-session-123456789",
        "page_size": 3,
    }
    await booking_alternatives_routes.booking_alternatives(BookingAlternativesRequest(**common), session)
    await booking_alternatives_routes.booking_alternatives(BookingAlternativesRequest(**common, offset=3), session)
    await booking_alternatives_routes.booking_alternatives(BookingAlternativesRequest(**common, offset=3), session)

    requested = [event for event in events if event["event_type"] == BookingRecoveryEventType.alternatives_requested]
    returned = [event for event in events if event["event_type"] == BookingRecoveryEventType.alternatives_returned]
    assert {event["event_key"] for event in requested} == {
        "alternatives-requested:anonymous-session-123456789:context-01HZY7QX6FD5"
    }
    assert [event["event_key"] for event in returned] == [
        "alternatives-returned:anonymous-session-123456789:context-01HZY7QX6FD5:0:3",
        "alternatives-returned:anonymous-session-123456789:context-01HZY7QX6FD5:3:3",
        "alternatives-returned:anonymous-session-123456789:context-01HZY7QX6FD5:3:3",
    ]
    assert [event["metric_value"] for event in returned] == [3, 2, 2]
    assert session.commits == 6
