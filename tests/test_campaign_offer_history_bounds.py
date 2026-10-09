"""Campaign history decisions on guarded, disposable PostgreSQL schemas.

The reused fixture requires an explicit local SEGMENTS_TEST_DATABASE_URL; these
tests call no providers and do not start the application lifespan.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.booking import BookingStatus
from app.models.campaign_offer import CampaignOffer
from app.services.campaign_offers import CampaignOfferService
from app.services.promotion import PromotionService
from tests.test_segments_integration import add_booking, add_customer, anyio_backend, database

NOW = datetime(2026, 9, 6, 9, tzinfo=UTC)
SNAPSHOT_AT = NOW - timedelta(days=30)
OLD_VISIT = NOW - timedelta(days=180)
FUTURE_VISIT = NOW + timedelta(days=1)


def offer_for(customer_id: int, mode: str) -> CampaignOffer:
    snapshot = {}
    if mode == "segments":
        snapshot = {
            "offer_audience_mode": "segments",
            "exclude_returned_since_snapshot": True,
            "exclude_upcoming_booking": False,
            "eligibility_evaluated_at": SNAPSHOT_AT.isoformat(),
        }
    # Only persisted customer/history are needed by current_restriction; the
    # offer carries an immutable audience snapshot and is never written here.
    return CampaignOffer(customer_id=customer_id, conditions_snapshot=snapshot)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "imported_at", "completed_ends", "expected"),
    [
        pytest.param("legacy", FUTURE_VISIT, [OLD_VISIT], None, id="legacy-future-import-past-local"),
        pytest.param("segments", FUTURE_VISIT, [OLD_VISIT], None, id="segments-future-import-past-local"),
        pytest.param("legacy", OLD_VISIT, [FUTURE_VISIT], None, id="legacy-future-local-past-import"),
        pytest.param("segments", OLD_VISIT, [FUTURE_VISIT], None, id="segments-future-local-past-import"),
        pytest.param("legacy", FUTURE_VISIT, [], "audience_no_longer_eligible", id="legacy-future-import-only"),
        pytest.param("segments", FUTURE_VISIT, [], None, id="segments-future-import-only"),
        pytest.param("legacy", None, [FUTURE_VISIT], "audience_no_longer_eligible", id="legacy-future-local-only"),
        pytest.param("segments", None, [FUTURE_VISIT], None, id="segments-future-local-only"),
        pytest.param("legacy", None, [], "audience_no_longer_eligible", id="legacy-no-history"),
        pytest.param("segments", None, [], None, id="segments-no-history"),
        pytest.param("legacy", OLD_VISIT, [], None, id="legacy-valid-past-import"),
        pytest.param("segments", OLD_VISIT, [], None, id="segments-past-import-before-snapshot"),
        pytest.param("legacy", OLD_VISIT, [NOW - timedelta(days=1)], "audience_no_longer_eligible", id="legacy-current-local-history"),
        pytest.param("segments", OLD_VISIT, [NOW - timedelta(days=1)], "returned_since_snapshot", id="segments-current-local-history"),
        pytest.param("segments", NOW, [], "returned_since_snapshot", id="import-at-evaluation-is-included"),
        pytest.param("segments", None, [NOW.astimezone(ZoneInfo("Europe/Kyiv"))], "returned_since_snapshot", id="local-at-equivalent-offset-is-included"),
        pytest.param("segments", NOW + timedelta(microseconds=1), [], None, id="import-just-after-evaluation-is-excluded"),
        pytest.param("segments", None, [NOW + timedelta(microseconds=1)], None, id="local-just-after-evaluation-is-excluded"),
        pytest.param("segments", SNAPSHOT_AT, [], None, id="import-at-snapshot-is-not-a-return"),
        pytest.param("segments", None, [SNAPSHOT_AT], None, id="local-at-snapshot-is-not-a-return"),
    ],
)
async def test_campaign_restrictions_use_history_existing_at_evaluation(
    database, mode, imported_at, completed_ends, expected
) -> None:
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=imported_at)
        for end_at in completed_ends:
            await add_booking(session, customer, end_at)
        await session.commit()

        restriction = await CampaignOfferService().current_restriction(
            session, offer_for(customer.id, mode), at=NOW
        )

        assert restriction == expected


@pytest.mark.anyio
async def test_campaign_bounds_keep_booking_promotion_history_semantics(database) -> None:
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=FUTURE_VISIT)
        await add_booking(session, customer, OLD_VISIT)
        await session.commit()

        assert await PromotionService().customer_last_visit_at(session, customer) == FUTURE_VISIT
        assert await CampaignOfferService().current_restriction(
            session, offer_for(customer.id, "legacy"), at=NOW
        ) is None


@pytest.mark.anyio
async def test_history_bounds_preserve_upcoming_booking_exclusion_and_frozen_options(database) -> None:
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=OLD_VISIT)
        upcoming = await add_booking(session, customer, FUTURE_VISIT, status=BookingStatus.confirmed)
        await session.commit()
        service = CampaignOfferService()

        legacy = offer_for(customer.id, "legacy")
        assert await service.current_restriction(session, legacy, at=NOW) == "future_booking"
        assert await service.current_restriction(
            session, legacy, at=NOW, exclude_booking_id=upcoming.id
        ) is None

        segment = offer_for(customer.id, "segments")
        assert await service.current_restriction(session, segment, at=NOW) is None
        segment.conditions_snapshot = {**segment.conditions_snapshot, "exclude_upcoming_booking": True}
        assert await service.current_restriction(session, segment, at=NOW) == "future_booking"
