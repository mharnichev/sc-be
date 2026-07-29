from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import sqlite

from app.models.booking_funnel import (
    BookingFunnelEventSource,
    BookingFunnelEventType,
    BookingFunnelWeeklyDigest,
)
from app.schemas.booking_funnel import PublicBookingFunnelEventCreate
from app.services.booking import KYIV_TZ
from app.services.booking_funnel import (
    BookingFunnelService,
    BookingFunnelThresholdConfig,
    WeeklyDigestResult,
    build_funnel_aggregate,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeResult:
    def __init__(self, *, scalar=None, rows=()):
        self.scalar = scalar
        self.rows = list(rows)

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar

    def all(self):
        return self.rows


class RecordingSession:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.added = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def add(self, item):
        self.added.append(item)
        if getattr(item, "id", None) is None:
            item.id = 91

    async def refresh(self, item):
        return None


def thresholds() -> BookingFunnelThresholdConfig:
    return BookingFunnelThresholdConfig(
        no_slot_min_count=2,
        no_slot_rate_percent=Decimal("25.00"),
        stale_schedule_count=2,
        booking_error_count=2,
        meaningful_step_sessions=2,
    )


def funnel_counts(**overrides: int) -> dict[BookingFunnelEventType, int]:
    counts = {event_type: 0 for event_type in BookingFunnelEventType}
    for key, value in overrides.items():
        counts[BookingFunnelEventType(key)] = value
    return counts


def test_public_contract_rejects_server_success_and_unstructured_personal_data() -> None:
    base = {
        "event_id": "evt-01HZY7QX6FD5",
        "anonymous_session_id": "session-01HZY7QX6FD5Q9BN",
    }
    with pytest.raises(ValidationError):
        PublicBookingFunnelEventCreate(
            **base,
            event_type=BookingFunnelEventType.booking_success,
        )
    with pytest.raises(ValidationError):
        PublicBookingFunnelEventCreate(
            **base,
            event_type=BookingFunnelEventType.booking_start,
            customer_phone="+380501112233",
        )
    with pytest.raises(ValidationError):
        PublicBookingFunnelEventCreate(
            **base,
            event_type=BookingFunnelEventType.slot_selected,
            target_date="2026-07-30",
        )
    with pytest.raises(ValidationError):
        PublicBookingFunnelEventCreate(
            **base,
            event_type=BookingFunnelEventType.no_slot,
            target_date="2026-02-31",
        )


@pytest.mark.anyio
async def test_public_event_idempotency_uses_conflict_safe_insert_and_hashes_session() -> None:
    session = RecordingSession([FakeResult(scalar=17), FakeResult(scalar=None)])
    service = BookingFunnelService(thresholds())
    anonymous_session_id = "session-01HZY7QX6FD5Q9BN"
    payload = PublicBookingFunnelEventCreate(
        event_id="evt-01HZY7QX6FD5",
        anonymous_session_id=anonymous_session_id,
        event_type=BookingFunnelEventType.no_slot,
        master_id=7,
        service_id=11,
        target_date="2026-07-30",
    )

    assert await service.record_public_event(session, payload) is True
    assert await service.record_public_event(session, payload) is False

    assert session.commits == 2
    assert session.rollbacks == 0
    compiled = session.statements[0].compile(dialect=sqlite.dialect())
    assert "ON CONFLICT DO NOTHING" in str(compiled)
    assert anonymous_session_id not in compiled.params.values()
    assert payload.event_id not in compiled.params.values()
    assert len(compiled.params["event_id_hash"]) == 64
    session_hash = compiled.params["anonymous_session_hash"]
    assert len(session_hash) == 64
    assert session_hash != anonymous_session_id
    assert compiled.params["target_date"] == date(2026, 7, 30)


@pytest.mark.anyio
async def test_aggregate_query_uses_half_open_kyiv_period_and_distinct_identities() -> None:
    start = datetime(2026, 3, 28, 0, 0, tzinfo=KYIV_TZ)
    end = datetime(2026, 3, 30, 0, 0, tzinfo=KYIV_TZ)
    session = RecordingSession(
        [
            FakeResult(
                rows=[
                    (BookingFunnelEventType.booking_start, 10, 0),
                    (BookingFunnelEventType.service_selected, 8, 0),
                    (BookingFunnelEventType.master_selected, 6, 0),
                    (BookingFunnelEventType.slot_selected, 3, 0),
                    (BookingFunnelEventType.contact_entered, 2, 0),
                    (BookingFunnelEventType.booking_success, 1, 0),
                    (BookingFunnelEventType.no_slot, 3, 0),
                ]
            ),
            FakeResult(
                rows=[
                    (
                        date(2026, 3, 29),
                        3,
                        2,
                        2,
                        datetime(2026, 3, 28, 10, 0, tzinfo=KYIV_TZ),
                        datetime(2026, 3, 29, 18, 0, tzinfo=KYIV_TZ),
                    ),
                    (
                        None,
                        1,
                        1,
                        1,
                        datetime(2026, 3, 28, 9, 0, tzinfo=KYIV_TZ),
                        datetime(2026, 3, 28, 9, 0, tzinfo=KYIV_TZ),
                    ),
                ]
            ),
        ]
    )

    aggregate = await BookingFunnelService(thresholds()).aggregate(
        session,
        start=start,
        end=end,
        master_id=7,
        include_latest_digest=False,
    )

    statement = session.statements[0]
    params = statement.compile().params.values()
    assert start in params
    assert end in params
    assert 7 in params
    assert start.utcoffset() != end.utcoffset()
    assert aggregate.overall_conversion is not None
    assert aggregate.overall_conversion.conversion_percent == Decimal("10.00")
    assert aggregate.no_slot_dates[0].target_date == date(2026, 3, 29)
    assert aggregate.no_slot_dates[0].observations == 3
    assert aggregate.no_slot_dates[0].unique_sessions == 2
    assert aggregate.no_slot_dates[0].affected_masters == 2
    assert aggregate.no_slot_unknown_date_count == 1
    no_slot_statement = session.statements[1]
    no_slot_params = no_slot_statement.compile().params.values()
    assert BookingFunnelEventType.no_slot in no_slot_params
    assert start in no_slot_params
    assert end in no_slot_params
    assert 7 in no_slot_params


def test_funnel_calculations_alerts_and_recommendation_are_deterministic() -> None:
    aggregate = build_funnel_aggregate(
        funnel_counts(
            booking_start=10,
            service_selected=8,
            master_selected=6,
            slot_selected=3,
            contact_entered=2,
            booking_success=1,
            no_slot=3,
            stale_schedule=1,
            booking_error=1,
        ),
        unattributed_booking_successes=0,
        thresholds=thresholds(),
    )

    assert aggregate.status == "available"
    assert [item.conversion_percent for item in aggregate.step_to_step_conversion] == [
        Decimal("80.00"),
        Decimal("75.00"),
        Decimal("50.00"),
        Decimal("66.67"),
        Decimal("50.00"),
    ]
    assert [item.count for item in aggregate.drop_offs] == [2, 2, 3, 1, 1]
    alerts = {item.code: item for item in aggregate.operational_alerts}
    assert alerts["no_slot"].count == 3
    assert alerts["no_slot"].rate_percent == Decimal("50.00")
    assert alerts["no_slot"].triggered is True
    assert alerts["stale_schedule"].triggered is False
    assert alerts["booking_error"].triggered is False
    assert aggregate.recommended_action is not None
    assert aggregate.recommended_action.code == "review_availability"
    assert aggregate.recommended_action.based_on == "no_slot"
    assert "10" in aggregate.weekly_insight_uk


def test_empty_and_incomplete_funnel_states_do_not_fabricate_conversion_data() -> None:
    empty = build_funnel_aggregate(
        funnel_counts(),
        unattributed_booking_successes=0,
        thresholds=thresholds(),
    )
    assert empty.status == "empty"
    assert empty.steps == []
    assert empty.overall_conversion is None
    assert empty.operational_alerts == []
    assert empty.no_slot_dates == []
    assert empty.no_slot_unknown_date_count == 0
    assert empty.recommended_action is None

    incomplete = build_funnel_aggregate(
        funnel_counts(service_selected=4, booking_success=1),
        unattributed_booking_successes=1,
        thresholds=thresholds(),
    )
    assert incomplete.status == "unavailable"
    assert incomplete.overall_conversion is not None
    assert incomplete.overall_conversion.status == "unavailable"
    assert incomplete.overall_conversion.conversion_percent is None


class StubAggregateService(BookingFunnelService):
    def __init__(self, aggregate):
        super().__init__(thresholds())
        self.aggregate_result = aggregate
        self.aggregate_calls = 0

    async def aggregate(self, *args, **kwargs):
        self.aggregate_calls += 1
        return self.aggregate_result


@pytest.mark.anyio
async def test_weekly_digest_persists_recommendation_and_completed_kyiv_week() -> None:
    aggregate = build_funnel_aggregate(
        funnel_counts(
            booking_start=10,
            service_selected=8,
            master_selected=6,
            slot_selected=3,
            contact_entered=2,
            booking_success=1,
            no_slot=3,
        ),
        unattributed_booking_successes=0,
        thresholds=thresholds(),
    )
    service = StubAggregateService(aggregate)
    session = RecordingSession([FakeResult(scalar=None)])

    result = await service.generate_latest_completed_week(
        session,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=KYIV_TZ),
    )

    assert isinstance(result, WeeklyDigestResult)
    assert result.created is True
    assert result.digest.period_start == date(2026, 7, 13)
    assert result.digest.period_end == date(2026, 7, 19)
    assert result.digest.recommended_action_code == "review_availability"
    assert result.digest.payload_json["recommended_action"]["based_on"] == "no_slot"
    assert session.commits == 1
    assert service.aggregate_calls == 1


@pytest.mark.anyio
async def test_weekly_digest_is_idempotent_when_period_already_exists() -> None:
    existing = BookingFunnelWeeklyDigest(
        id=41,
        period_start=date(2026, 7, 13),
        period_end=date(2026, 7, 19),
        generated_at=datetime(2026, 7, 20, 1, 0, tzinfo=KYIV_TZ),
        data_status="empty",
        insight_uk="Немає подій.",
        payload_json={},
    )
    aggregate = build_funnel_aggregate(
        funnel_counts(),
        unattributed_booking_successes=0,
        thresholds=thresholds(),
    )
    service = StubAggregateService(aggregate)
    session = RecordingSession([FakeResult(scalar=existing)])

    result = await service.generate_latest_completed_week(
        session,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=KYIV_TZ),
    )

    assert isinstance(result, WeeklyDigestResult)
    assert result.created is False
    assert result.digest.id == 41
    assert service.aggregate_calls == 0
    assert session.commits == 1


def test_openapi_exposes_public_event_contract_and_dashboard_funnel() -> None:
    from app.main import app

    schema = app.openapi()
    public_operation = schema["paths"]["/api/v1/public/booking-funnel/events"]["post"]
    dashboard_schema = schema["components"]["schemas"]["AdminDashboardStatisticsResponse"]

    assert public_operation["summary"] == "Record a privacy-safe booking funnel event"
    public_event_schema = schema["components"]["schemas"]["PublicBookingFunnelEventCreate"]
    assert "target_date" in public_event_schema["properties"]
    assert "booking_funnel" in dashboard_schema["properties"]
    aggregate_schema = schema["components"]["schemas"]["BookingFunnelAggregate"]
    assert "no_slot_dates" in aggregate_schema["properties"]
    assert "no_slot_unknown_date_count" in aggregate_schema["properties"]
    assert "funnel_session_id" in schema["components"]["schemas"]["PublicBookingCreate"]["properties"]
