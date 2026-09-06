import asyncio
import gc
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy import or_, select

from app.models.customer import Customer
from app.schemas.segment import SegmentPeriod, SegmentPreviewRequest, SegmentRules, SegmentUpdate
from app.services.segments import SegmentService, evaluation_timestamp, period_bounds, subtract_age


KYIV = ZoneInfo("Europe/Kyiv")


@pytest.mark.parametrize("year,day", [(2024, 29), (2025, 28)])
def test_calendar_month_clamps_to_last_day_without_converting_to_days(year, day):
    at = datetime(year, 3, 31, 12, 30, tzinfo=KYIV)
    assert subtract_age(at, 1, "calendar_months").astimezone(KYIV) == datetime(year, 2, day, 12, 30, tzinfo=KYIV)
    assert subtract_age(at, 1, "calendar_months") != subtract_age(at, 30, "days")


def test_calendar_month_preserves_kyiv_wall_clock_across_dst():
    at = datetime(2025, 4, 15, 12, tzinfo=KYIV)
    shifted = subtract_age(at, 1, "calendar_months")
    assert shifted == datetime(2025, 3, 15, 10, tzinfo=UTC)
    assert subtract_age(at, 31, "days") == at.astimezone(UTC) - timedelta(days=31)
    assert shifted != subtract_age(at, 31, "days")


def test_period_uses_same_evaluation_instant_and_clamps_future_end():
    at = datetime(2026, 9, 6, 9, tzinfo=UTC)
    assert period_bounds(SegmentPeriod(last=3, unit="calendar_months"), at) == (
        datetime(2026, 6, 6, 9, tzinfo=UTC), at,
    )
    period = SegmentPeriod(start=at - timedelta(days=1), end=at + timedelta(days=1))
    assert period_bounds(period, at) == (at - timedelta(days=1), at)


def test_evaluation_normalizes_offsets_and_rejects_naive_timestamp():
    assert evaluation_timestamp(datetime(2026, 9, 6, 12, tzinfo=KYIV)) == datetime(2026, 9, 6, 9, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        evaluation_timestamp(datetime(2026, 9, 6))


@pytest.mark.parametrize("condition", [
    {"type": "last_visit_age"},
    {"type": "last_visit_age", "min": 12, "max": 3},
    {"type": "last_visit_age", "min": 1201},
    {"type": "completed_visit_count", "min": 3, "max": 1},
    {"type": "visited_master", "master_ids": [0]},
    {"type": "visited_master", "master_ids": [1], "mode": "within_period"},
    {"type": "received_service", "service_ids": [-1], "period": {"last": 30}},
    {"type": "upcoming_booking", "sql": "select * from customers"},
    {"type": "executable", "expression": "True"},
])
def test_rejects_invalid_or_executable_rules(condition):
    with pytest.raises(ValidationError):
        SegmentRules(conditions=[condition])


def test_rule_depth_and_size_are_bounded():
    with pytest.raises(ValidationError):
        SegmentRules(conditions=[])
    with pytest.raises(ValidationError):
        SegmentRules(conditions=[{"type": "upcoming_booking"}] * 21)
    with pytest.raises(ValidationError):
        SegmentRules(conditions=[{"combine": "all", "conditions": [{"type": "upcoming_booking"}]}])


@pytest.mark.parametrize("period", [
    {}, {"last": 0}, {"last": 121, "unit": "calendar_months"},
    {"start": "2026-01-01T00:00:00", "end": "2026-02-01T00:00:00"},
    {"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
    {"last": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
])
def test_period_validation(period):
    with pytest.raises(ValidationError):
        SegmentPeriod(**period)


def test_update_requires_revision_and_disallows_erasing_rules():
    with pytest.raises(ValidationError):
        SegmentUpdate(name="Changed")
    with pytest.raises(ValidationError):
        SegmentUpdate(expected_revision=1, rules=None)
    with pytest.raises(ValidationError):
        SegmentUpdate(expected_revision=1, name="   ")


def test_preview_rejects_naive_time():
    with pytest.raises(ValidationError):
        SegmentPreviewRequest(rules={"conditions": [{"type": "upcoming_booking"}]}, evaluated_at="2026-09-06")


def test_full_rule_compiler_produces_postgresql_statement_without_clock_functions():
    period = {"last": 6, "unit": "calendar_months"}
    rules = SegmentRules(conditions=[
        {"type": "last_visit_age", "min": 3, "max": 12},
        {"type": "completed_visit_count", "min": 1, "period": period},
        {"type": "visited_master", "master_ids": [1], "mode": "last"},
        {"type": "visited_master", "master_ids": [2], "mode": "within_period", "period": period},
        {"type": "received_service", "service_ids": [4], "period": period},
        {"type": "first_visit", "period": period},
        {"type": "received_campaign", "campaign_id": 3},
        {"type": "marketing_contact", "period": period, "present": False},
    ], exclusions=[{"type": "upcoming_booking"}])
    sql = str(SegmentService().build_customer_statement(
        rules, datetime(2026, 9, 6, 9, tzinfo=UTC)
    ).compile(dialect=postgresql.dialect()))
    assert "now()" not in sql.lower()
    assert "booking_service_items" in sql
    assert "row_number() OVER" in sql


def test_facts_enforce_bounded_batch_before_querying():
    with pytest.raises(ValueError, match="500"):
        asyncio.run(SegmentService().member_facts(
            None, list(range(501)), {"conditions": [{"type": "upcoming_booking"}]}, datetime.now(UTC),
        ))


@pytest.mark.parametrize("at,unit", [
    (datetime(1, 1, 1, tzinfo=UTC), "days"),
    (datetime(1, 1, 1, tzinfo=UTC), "calendar_months"),
])
def test_preview_out_of_range_age_is_validation_error_before_database_access(at, unit):
    with pytest.raises(HTTPException) as error:
        asyncio.run(SegmentService().preview(
            None, {"conditions": [{"type": "last_visit_age", "min": 3, "unit": unit}]}, evaluated_at=at,
        ))
    assert error.value.status_code == 422


def test_identical_segment_predicates_compile_together_across_repeated_builds():
    service = SegmentService()
    rules = SegmentRules(conditions=[{"type": "last_visit_age", "min": 3, "max": 12}])
    at = datetime(2026, 9, 6, 9, tzinfo=UTC)
    for _ in range(10):
        statement = select(Customer).where(or_(*(service.build_predicate(rules, at) for _ in range(20))))
        statement.compile(dialect=postgresql.dialect())
        gc.collect()
