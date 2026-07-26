from datetime import UTC, datetime, timedelta

from app.services.service_popularity import calculate_popularity_ranks, is_refresh_due


def test_service_popularity_ranks_completed_booking_counts() -> None:
    assert calculate_popularity_ranks({1: 3, 2: 12, 3: 0, 4: 7}) == {
        2: 1,
        4: 2,
        1: 3,
    }


def test_service_popularity_ranks_ties_deterministically() -> None:
    assert calculate_popularity_ranks({7: 4, 3: 4}) == {
        3: 1,
        7: 2,
    }


def test_service_popularity_refresh_is_due_monthly() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    assert not is_refresh_due(now - timedelta(days=29), now, 30)
    assert is_refresh_due(now - timedelta(days=30), now, 30)
    assert is_refresh_due(None, now, 30)
