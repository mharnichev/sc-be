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
async def test_client_event_id_cannot_collide_with_server_booking_event() -> None:
    service = BookingFunnelService(thresholds())
    client_session = RecordingSession([FakeResult(scalar=17)])
    await service.record_public_event(
        client_session,
        PublicBookingFunnelEventCreate(
            event_id="server:booking:41",
            anonymous_session_id="session-01HZY7QX6FD5Q9BN",
            event_type=BookingFunnelEventType.booking_start,
        ),
    )
    client_hash = (
        client_session.statements[0]
        .compile(dialect=sqlite.dialect())
        .params["event_id_hash"]
    )

    server_session = RecordingSession([])
    server_event = service.add_booking_success(
        server_session,
        booking_id=41,
        master_id=7,
        service_id=11,
        anonymous_session_id="session-01HZY7QX6FD5Q9BN",
    )

    assert client_hash != server_event.event_id_hash
    assert len({event.event_id_hash for event in server_session.added}) == 6


@pytest.mark.anyio
async def test_aggregate_query_uses_half_open_kyiv_period_and_distinct_identities() -> None:
    start = datetime(2026, 3, 28, 0, 0, tzinfo=KYIV_TZ)
    end = datetime(2026, 3, 30, 0, 0, tzinfo=KYIV_TZ)
    session = RecordingSession(
        [
            FakeResult(
                rows=[
                    *[
                        (BookingFunnelEventType.booking_start, f"s{index}", None, None)
                        for index in range(1, 11)
                    ],
                    *[
                        (BookingFunnelEventType.service_selected, f"s{index}", None, None)
                        for index in range(1, 9)
                    ],
                    *[
                        (BookingFunnelEventType.master_selected, f"s{index}", 7, None)
                        for index in range(1, 7)
                    ],
                    *[
                        (BookingFunnelEventType.slot_selected, f"s{index}", 7, None)
                        for index in range(1, 4)
                    ],
                    (BookingFunnelEventType.contact_entered, "s1", 7, None),
                    (BookingFunnelEventType.contact_entered, "s2", 7, None),
                    (BookingFunnelEventType.booking_success, "s1", 7, 41),
                    (BookingFunnelEventType.booking_success, "s1", 7, 42),
                ]
            ),
            FakeResult(
                rows=[
                    *[
                        (BookingFunnelEventType.master_selected, f"s{index}", 7, None)
                        for index in range(1, 7)
                    ],
                    *[
                        (BookingFunnelEventType.no_slot, f"s{index}", 7, None)
                        for index in range(1, 4)
                    ],
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
    assert start.utcoffset() != end.utcoffset()
    assert aggregate.overall_conversion is not None
    assert aggregate.overall_conversion.conversion_percent == Decimal("16.67")
    assert aggregate.no_slot_dates[0].target_date == date(2026, 3, 29)
    assert aggregate.no_slot_dates[0].observations == 3
    assert aggregate.no_slot_dates[0].unique_sessions == 2
    assert aggregate.no_slot_dates[0].affected_masters == 2
    assert aggregate.no_slot_unknown_date_count == 1
    no_slot_statement = session.statements[2]
    no_slot_params = no_slot_statement.compile().params.values()
    assert BookingFunnelEventType.no_slot in no_slot_params
    assert start in no_slot_params
    assert end in no_slot_params
    assert 7 in no_slot_params
    assert aggregate.tracking_gap_count == 0


@pytest.mark.anyio
async def test_unattributed_success_survives_deleted_booking_foreign_key() -> None:
    start = datetime(2026, 7, 1, 0, 0, tzinfo=KYIV_TZ)
    end = datetime(2026, 8, 1, 0, 0, tzinfo=KYIV_TZ)
    session = RecordingSession(
        [
            FakeResult(rows=[]),
            FakeResult(
                rows=[
                    (
                        BookingFunnelEventType.booking_success,
                        None,
                        7,
                        501,
                    ),
                ]
            ),
            FakeResult(rows=[]),
        ]
    )

    aggregate = await BookingFunnelService(thresholds()).aggregate(
        session,
        start=start,
        end=end,
        include_latest_digest=False,
    )

    period_sql = str(session.statements[1])
    assert "booking_funnel_events.id" in period_sql
    assert "booking_funnel_events.booking_id" not in period_sql
    assert aggregate.unattributed_booking_successes == 1
    assert aggregate.status == "unavailable"


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


def test_operational_recommendation_is_ranked_against_its_own_threshold() -> None:
    aggregate = build_funnel_aggregate(
        funnel_counts(
            booking_start=100,
            service_selected=10,
            master_selected=10,
            slot_selected=10,
            contact_entered=10,
            booking_success=10,
            booking_error=2,
        ),
        unattributed_booking_successes=0,
        thresholds=thresholds(),
    )

    assert aggregate.step_to_step_conversion[0].conversion_percent == Decimal("10.00")
    assert aggregate.recommended_action is not None
    assert aggregate.recommended_action.code == "investigate_booking_errors"
    assert aggregate.recommended_action.based_on == "booking_error"


def test_funnel_conversion_uses_same_session_intersections() -> None:
    aggregate = build_funnel_aggregate(
        funnel_counts(
            booking_start=2,
            service_selected=2,
            master_selected=1,
            slot_selected=1,
            contact_entered=1,
            booking_success=1,
        ),
        transition_counts={
            (
                BookingFunnelEventType.booking_start,
                BookingFunnelEventType.service_selected,
            ): 0,
            (
                BookingFunnelEventType.service_selected,
                BookingFunnelEventType.master_selected,
            ): 1,
            (
                BookingFunnelEventType.master_selected,
                BookingFunnelEventType.slot_selected,
            ): 1,
            (
                BookingFunnelEventType.slot_selected,
                BookingFunnelEventType.contact_entered,
            ): 1,
            (
                BookingFunnelEventType.contact_entered,
                BookingFunnelEventType.booking_success,
            ): 1,
        },
        overall_success_sessions=0,
        tracking_gaps={
            (
                BookingFunnelEventType.booking_start,
                BookingFunnelEventType.service_selected,
            ): 2,
        },
        unattributed_booking_successes=0,
        thresholds=thresholds(),
    )

    first_transition = aggregate.step_to_step_conversion[0]
    assert first_transition.from_count == 2
    assert first_transition.to_count == 0
    assert first_transition.conversion_percent == Decimal("0.00")
    assert aggregate.overall_conversion is not None
    assert aggregate.overall_conversion.succeeded == 0
    assert aggregate.overall_conversion.conversion_percent == Decimal("0.00")
    assert aggregate.status == "partial"
    assert aggregate.tracking_gap_count == 2


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

    unattributed_only = build_funnel_aggregate(
        funnel_counts(),
        unattributed_booking_successes=2,
        thresholds=thresholds(),
    )
    assert unattributed_only.status == "unavailable"
    assert unattributed_only.unattributed_booking_successes == 2
    assert unattributed_only.overall_conversion is not None
    assert unattributed_only.overall_conversion.conversion_percent is None


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
async def test_weekly_digest_recalculates_existing_period_as_attempts_mature() -> None:
    existing = BookingFunnelWeeklyDigest(
        id=41,
        period_start=date(2026, 7, 13),
        period_end=date(2026, 7, 19),
        generated_at=datetime(2026, 7, 20, 1, 0, tzinfo=KYIV_TZ),
        data_status="empty",
        insight_uk="Немає подій.",
        payload_json={"calculation_version": 2},
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
    assert service.aggregate_calls == 1
    assert result.digest.generated_at == datetime(2026, 7, 22, 12, 0, tzinfo=KYIV_TZ)
    assert result.digest.payload_json["calculation_version"] == 2
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
