"""PostgreSQL-only recovery analytics checks against an explicitly isolated DB."""

from datetime import date, datetime
import os

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.booking_funnel import BookingFunnelEvent
from app.models.booking_recovery import BookingRecoveryEvent, BookingRecoveryEventType
from app.services.booking_recovery_analytics import BookingRecoveryAnalyticsService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def database():
    url = os.environ.get("BOOKING_ANALYTICS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set BOOKING_ANALYTICS_TEST_DATABASE_URL to an isolated local PostgreSQL database")
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1", "::1", "postgres"} or "analytics_test" not in (parsed.database or ""):
        pytest.fail("Analytics integration database must be the isolated booking_analytics_test database")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(delete(BookingRecoveryEvent))
            await connection.execute(delete(BookingFunnelEvent))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_cohort_keeps_session_context_pair_and_counts_late_completion_once(database):
    service = BookingRecoveryAnalyticsService()
    at = datetime(2026, 10, 2, 12, 0)

    async def record(session, event_type, key, session_id, context_id, occurred_at=at):
        return await service.record(
            session,
            event_type=event_type,
            event_key=key,
            anonymous_session_id=session_id,
            search_context_id=context_id,
            offer_id="offer-1234567890",
            occurred_at=occurred_at,
        )

    async with database() as session:
        await record(session, BookingRecoveryEventType.alternatives_requested, "request-a", "session-a-123456789", "context-a-123456789")
        await record(session, BookingRecoveryEventType.alternatives_requested, "request-b", "session-b-123456789", "context-b-123456789")
        await record(session, BookingRecoveryEventType.alternative_slot_viewed, "view-b", "session-b-123456789", "context-b-123456789")
        # Neither mismatch can be attributed to a request in the report cohort.
        await record(session, BookingRecoveryEventType.alternative_slot_selected, "selected-cross", "session-a-123456789", "context-b-123456789")
        await record(session, BookingRecoveryEventType.alternative_slot_selected, "selected-no-view", "session-a-123456789", "context-a-123456789")
        await record(session, BookingRecoveryEventType.alternative_slot_selected, "selected-b", "session-b-123456789", "context-b-123456789")
        await record(session, BookingRecoveryEventType.booking_completed_after_alternative, "completed-cross", "session-a-123456789", "context-b-123456789", datetime(2026, 10, 8, 12, 0))
        await record(session, BookingRecoveryEventType.booking_completed_after_alternative, "completed-b", "session-b-123456789", "context-b-123456789", datetime(2026, 10, 8, 12, 0))
        assert not await record(session, BookingRecoveryEventType.alternative_slot_viewed, "view-b", "session-b-123456789", "context-b-123456789")
        await session.commit()

        summary = await service.summary(session, date_from=date(2026, 10, 1), date_to=date(2026, 10, 4))

    assert (
        summary.alternative_cohort_requested_sessions,
        summary.alternative_cohort_viewed_sessions,
        summary.alternative_cohort_selected_sessions,
        summary.alternative_cohort_completed_sessions,
    ) == (2, 1, 1, 1)
    assert summary.alternative_cohort_selection_rate_percent == 100
    assert summary.alternative_cohort_completion_rate_percent == 50
