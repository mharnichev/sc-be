from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.routes import statistics as statistics_routes
from app.services.admin_dashboard_statistics import (
    MAX_DASHBOARD_RANGE_DAYS,
    AdminDashboardStatisticsService,
    CapacityByMaster,
    DashboardSignalThresholdConfig,
    ExecutiveAggregate,
    RetentionAggregate,
    build_actionable_signals,
    merge_intervals,
    percent,
    safe_percent_change,
    subtract_intervals,
)
from app.services.booking import KYIV_TZ
from app.services.master_reviews import ReviewOperationalCounts


class FakeResult:
    def __init__(self, *, rows=(), one=None):
        self._rows = list(rows)
        self._one = one

    def all(self):
        return self._rows

    def one(self):
        return self._one


class SequenceSession:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=KYIV_TZ)


def empty_executive() -> ExecutiveAggregate:
    return ExecutiveAggregate(
        gross_revenue=Decimal("0.00"),
        completed_visits=0,
        unique_clients=0,
        average_check=Decimal("0.00"),
        booking_subtotal=Decimal("0.00"),
        promotion_discount_amount=Decimal("0.00"),
        cancelled_visits=0,
        scheduled_bookings=0,
        pending_upcoming_bookings=0,
    )


def test_dashboard_range_is_inclusive_and_uses_kyiv_calendar_bounds() -> None:
    service = AdminDashboardStatisticsService()

    bounds = service.period_bounds(date(2026, 3, 28), date(2026, 3, 29))

    assert bounds.days == 2
    assert bounds.start == datetime(2026, 3, 28, 0, 0, tzinfo=KYIV_TZ)
    assert bounds.end == datetime(2026, 3, 30, 0, 0, tzinfo=KYIV_TZ)
    assert bounds.start.utcoffset() == timedelta(hours=2)
    assert bounds.end.utcoffset() == timedelta(hours=3)


def test_dashboard_range_validation_and_equal_previous_period() -> None:
    service = AdminDashboardStatisticsService()
    current = service.period_bounds(date(2026, 6, 10), date(2026, 6, 20))
    previous = service.previous_period(current)

    assert previous.date_from == date(2026, 5, 30)
    assert previous.date_to == date(2026, 6, 9)
    assert previous.days == current.days == 11

    with pytest.raises(HTTPException) as reversed_range:
        service.period_bounds(date(2026, 6, 20), date(2026, 6, 10))
    assert reversed_range.value.status_code == 422

    with pytest.raises(HTTPException) as oversized_range:
        service.period_bounds(
            date(2026, 1, 1),
            date(2026, 1, 1) + timedelta(days=MAX_DASHBOARD_RANGE_DAYS),
        )
    assert oversized_range.value.status_code == 422


def test_comparison_math_is_decimal_safe_and_zero_denominator_is_unavailable() -> None:
    assert safe_percent_change(Decimal("120.00"), Decimal("100.00")) == Decimal("20.00")
    assert safe_percent_change(80, 100) == Decimal("-20.00")
    assert safe_percent_change(10, 0) is None
    assert safe_percent_change(10, None) is None
    assert percent(1, 3) == Decimal("33.33")
    assert percent(0, 0) == Decimal("0.00")


def test_interval_math_unions_overlapping_blocks_before_subtraction() -> None:
    availability = [(at(1, 9), at(1, 20))]
    overlapping_blocks = [(at(1, 12), at(1, 14)), (at(1, 13), at(1, 15))]

    assert merge_intervals(overlapping_blocks) == [(at(1, 12), at(1, 15))]
    assert subtract_intervals(availability, overlapping_blocks) == [
        (at(1, 9), at(1, 12)),
        (at(1, 15), at(1, 20)),
    ]


@pytest.mark.anyio
async def test_capacity_subtracts_blocks_and_counts_only_bookings_inside_availability() -> None:
    service = AdminDashboardStatisticsService()
    period = service.period_bounds(date(2026, 7, 1), date(2026, 7, 1))
    session = SequenceSession(
        [
            FakeResult(rows=[(7, at(1, 9), at(1, 20))]),
            FakeResult(rows=[(7, at(1, 12), at(1, 13))]),
            FakeResult(
                rows=[
                    (7, at(1, 17), at(1, 18)),
                    (7, at(1, 7), at(1, 8)),
                ]
            ),
        ]
    )

    by_master, prime_windows = await service._capacity(
        session,  # type: ignore[arg-type]
        period=period,
        masters=[(7, "Gleb")],
        now=at(1, 8),
    )

    assert by_master[7] == CapacityByMaster(
        available_minutes=600,
        booked_minutes=60,
        upcoming_available_minutes=600,
        upcoming_empty_minutes=540,
    )
    assert len(prime_windows) == 1
    assert prime_windows[0].start_at == at(1, 18)
    assert prime_windows[0].end_at == at(1, 20)
    assert prime_windows[0].available_minutes == 120


@pytest.mark.anyio
async def test_executive_snapshots_include_discounts_and_master_filter() -> None:
    service = AdminDashboardStatisticsService()
    period = service.period_bounds(date(2026, 7, 1), date(2026, 7, 31))
    session = SequenceSession(
        [
            FakeResult(
                one=(
                    Decimal("900.00"),
                    2,
                    2,
                    Decimal("1000.00"),
                    Decimal("100.00"),
                    1,
                    4,
                    1,
                )
            )
        ]
    )

    result = await service._executive_aggregate(
        session,  # type: ignore[arg-type]
        period=period,
        master_id=7,
        now=at(1, 8),
    )

    assert result.gross_revenue == Decimal("900.00")
    assert result.booking_subtotal == Decimal("1000.00")
    assert result.promotion_discount_amount == Decimal("100.00")
    assert result.average_check == Decimal("450.00")
    assert result.cancelled_visits == 1
    assert result.scheduled_bookings == 4
    assert result.pending_upcoming_bookings == 1
    assert "bookings.master_id" in str(session.statements[0])


@pytest.mark.anyio
async def test_service_breakdown_returns_realized_snapshot_amounts_without_cost_claims() -> None:
    service = AdminDashboardStatisticsService()
    period = service.period_bounds(date(2026, 7, 1), date(2026, 7, 31))
    session = SequenceSession(
        [
            FakeResult(
                rows=[
                    (
                        3,
                        "Haircut",
                        2,
                        Decimal("900.00"),
                        Decimal("1000.00"),
                        Decimal("100.00"),
                    )
                ]
            )
        ]
    )

    result = await service._service_breakdown(
        session,  # type: ignore[arg-type]
        period=period,
        master_id=None,
    )

    assert len(result) == 1
    assert result[0].completed_visits == 2
    assert result[0].gross_revenue == Decimal("900.00")
    assert result[0].subtotal == Decimal("1000.00")
    assert result[0].discounts == Decimal("100.00")
    assert result[0].average_realized_revenue_per_completed_service == Decimal("450.00")
    assert "margin" not in type(result[0]).model_fields
    statement = str(session.statements[0])
    assert "UNION ALL" in statement
    assert "bookings.service_id" in statement
    assert "EXISTS" in statement


@pytest.mark.anyio
async def test_review_rating_aggregate_is_approved_only_and_bulk() -> None:
    service = AdminDashboardStatisticsService()
    session = SequenceSession(
        [
            FakeResult(
                rows=[
                    (7, Decimal("4.46"), 12),
                    (8, Decimal("5.00"), 2),
                ]
            )
        ]
    )

    result = await service.review_service.approved_rating_aggregates(
        session,  # type: ignore[arg-type]
        [8, 7, 7],
    )

    assert result[7].average_rating == Decimal("4.5")
    assert result[7].review_count == 12
    assert result[8].average_rating == Decimal("5.0")
    assert len(session.statements) == 1
    assert "master_reviews.status" in str(session.statements[0])


@pytest.mark.anyio
async def test_empty_dashboard_is_typed_and_no_show_is_unavailable(monkeypatch) -> None:
    service = AdminDashboardStatisticsService()

    async def visible(*args, **kwargs):
        return []

    async def executive(*args, **kwargs):
        return empty_executive()

    async def capacity(*args, **kwargs):
        return {}, []

    async def retention(*args, **kwargs):
        return RetentionAggregate(
            new_clients=0,
            returning_clients=0,
            repeat_metrics={30: (0, 0), 45: (0, 0), 60: (0, 0)},
            by_master={},
        )

    async def financials(*args, **kwargs):
        return {}

    async def services(*args, **kwargs):
        return []

    async def ratings(*args, **kwargs):
        return {}

    async def review_counts(*args, **kwargs):
        return ReviewOperationalCounts(moderation_backlog=0, failed_deliveries=0)

    monkeypatch.setattr(service, "_visible_masters", visible)
    monkeypatch.setattr(service, "_executive_aggregate", executive)
    monkeypatch.setattr(service, "_capacity", capacity)
    monkeypatch.setattr(service, "_retention", retention)
    monkeypatch.setattr(service, "_master_financials", financials)
    monkeypatch.setattr(service, "_service_breakdown", services)
    monkeypatch.setattr(service.review_service, "approved_rating_aggregates", ratings)
    monkeypatch.setattr(service.review_service, "dashboard_operational_counts", review_counts)

    response = await service.get_dashboard(
        SimpleNamespace(),
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 1),
        compare_to_previous=True,
        now=at(1, 8),
    )

    assert response.executive.gross_revenue.current == Decimal("0.00")
    assert response.executive.gross_revenue.previous == Decimal("0.00")
    assert response.executive.gross_revenue.percent_change is None
    assert response.capacity_and_leakage.no_show_visits is None
    assert response.capacity_and_leakage.no_show_status == "unavailable"
    assert response.retention.repeat_30_day.eligible_clients == 0
    assert response.retention.repeat_30_day.repeat_rate is None
    assert response.masters == []
    assert response.services == []
    assert response.actionable_signals == []


@pytest.mark.anyio
async def test_retention_returns_eligible_denominators_for_every_window() -> None:
    service = AdminDashboardStatisticsService()
    period = service.period_bounds(date(2026, 1, 1), date(2026, 3, 31))
    session = SequenceSession(
        [
            FakeResult(one=(4, 3)),
            FakeResult(rows=[(7, 2, 1), (8, 2, 2)]),
            FakeResult(one=(3, 5, 2, 4, 1, 2)),
        ]
    )

    result = await service._retention(
        session,  # type: ignore[arg-type]
        period=period,
        master_id=None,
        now=datetime(2026, 3, 15, 12, 0, tzinfo=KYIV_TZ),
    )

    assert result.new_clients == 4
    assert result.returning_clients == 3
    assert result.repeat_metrics == {30: (3, 5), 45: (2, 4), 60: (1, 2)}
    assert result.by_master == {7: (2, 1), 8: (2, 2)}
    repeat_sql = str(session.statements[2])
    assert "first_visit <=" in repeat_sql
    assert "second_visit <=" in repeat_sql


def test_action_signal_thresholds_are_inclusive_and_deterministic() -> None:
    thresholds = DashboardSignalThresholdConfig()
    below = build_actionable_signals(
        pending_upcoming=0,
        cancelled_visits=thresholds.cancellation_min_count - 1,
        cancellation_rate=thresholds.cancellation_min_rate_percent,
        previous_cancellation_rate=Decimal("0.00"),
        empty_upcoming_minutes=thresholds.unfilled_capacity_min_minutes - 1,
        empty_upcoming_rate=thresholds.unfilled_capacity_min_percent,
        moderation_backlog=0,
        failed_review_deliveries=0,
    )
    assert below == []

    at_threshold = build_actionable_signals(
        pending_upcoming=thresholds.pending_bookings_min_count,
        cancelled_visits=thresholds.cancellation_min_count,
        cancellation_rate=thresholds.cancellation_min_rate_percent,
        previous_cancellation_rate=(
            thresholds.cancellation_min_rate_percent
            - thresholds.cancellation_min_increase_percentage_points
        ),
        empty_upcoming_minutes=thresholds.unfilled_capacity_min_minutes,
        empty_upcoming_rate=thresholds.unfilled_capacity_min_percent,
        moderation_backlog=thresholds.review_moderation_backlog_min_count,
        failed_review_deliveries=thresholds.failed_review_delivery_min_count,
    )

    assert [item.code for item in at_threshold] == [
        "pending_bookings",
        "elevated_cancellations",
        "unfilled_capacity",
        "review_moderation_backlog",
        "failed_review_delivery",
    ]
    assert [item.recommended_backoffice_route for item in at_threshold] == [
        "/bookings?status=pending",
        "/bookings?status=cancelled",
        "/time-blocks",
        "/reviews?moderation_status=pending",
        "/reviews?request_state=failed",
    ]


@pytest.mark.anyio
async def test_dashboard_route_is_superuser_only_and_forwards_master_filter(monkeypatch) -> None:
    class FakeDashboardService:
        called_with = None

        async def get_dashboard(self, session, **kwargs):
            self.called_with = kwargs
            return "dashboard"

    fake = FakeDashboardService()
    monkeypatch.setattr(statistics_routes, "admin_dashboard_statistics_service", fake)

    with pytest.raises(HTTPException) as forbidden:
        await statistics_routes.get_admin_dashboard_statistics(
            date_from=date(2026, 7, 1),
            date_to=date(2026, 7, 31),
            compare_to_previous=True,
            master_id=7,
            current_user=SimpleNamespace(is_superuser=False),
            session=SimpleNamespace(),
        )
    assert forbidden.value.status_code == 403
    assert fake.called_with is None

    result = await statistics_routes.get_admin_dashboard_statistics(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 31),
        compare_to_previous=False,
        master_id=7,
        current_user=SimpleNamespace(is_superuser=True),
        session=SimpleNamespace(),
    )
    assert result == "dashboard"
    assert fake.called_with == {
        "date_from": date(2026, 7, 1),
        "date_to": date(2026, 7, 31),
        "compare_to_previous": False,
        "master_id": 7,
    }


def test_openapi_documents_dashboard_contract_and_money_as_revenue() -> None:
    from app.main import app

    operation = app.openapi()["paths"]["/api/v1/backoffice/statistics/admin/dashboard"]["get"]

    assert operation["summary"] == "Owner revenue, capacity, retention and leakage dashboard"
    assert {item["name"] for item in operation["parameters"]} == {
        "date_from",
        "date_to",
        "compare_to_previous",
        "master_id",
    }
    assert "profit" in operation["description"].lower()
