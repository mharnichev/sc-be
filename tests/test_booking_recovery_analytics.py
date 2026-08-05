from datetime import date

import pytest

from app.models.booking_recovery import BookingRecoveryEventType
from app.services.booking_recovery_analytics import BookingRecoveryAnalyticsService


class AnalyticsResult:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one(self):
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
                AnalyticsResult(rows=[(2, 90)]),
            ]
        ),
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 5),
    )

    assert summary.no_slot_sessions == 6
    assert summary.alternative_recovery_rate_percent == 25
    assert summary.alternative_slots_returned == 8
    assert summary.waitlist_requests == 5
    assert summary.offers_claimed == 2
    assert summary.cancelled_slots_refilled == 2
    assert summary.average_cancellation_to_refill_seconds == 90
    assert "phone" not in summary.model_dump()
