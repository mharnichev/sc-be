"""Moderation audit lineage in isolated PostgreSQL, including stale identities."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.models.booking import BarberService, Booking, Master
from app.models.master_review import MasterReview, MasterReviewModerationAudit, MasterReviewStatus
from app.services.master_reviews import MasterReviewService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401

pytestmark = pytest.mark.anyio


async def seed_review(database):
    async with database() as session:
        master = Master(full_name="Synthetic moderation master")
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Synthetic moderation service", duration_minutes=30, price=100)
        session.add(service)
        await session.flush()
        now = datetime.now(UTC)
        booking = Booking(master_id=master.id, service_id=service.id, customer_name="Synthetic",
                          customer_phone="+380500000001", start_at=now, end_at=now + timedelta(minutes=30))
        session.add(booking)
        await session.flush()
        review = MasterReview(booking_id=booking.id, master_id=master.id, rating=5, submitted_at=now)
        session.add(review)
        await session.commit()
        return review.id


async def stub_detail(_session, review_id):
    # Only the post-commit DTO read is stubbed; row writes and audit are real.
    return review_id


@pytest.mark.parametrize("first,second", [
    (MasterReviewStatus.approved, MasterReviewStatus.rejected),
    (MasterReviewStatus.rejected, MasterReviewStatus.approved),
    (MasterReviewStatus.approved, MasterReviewStatus.approved),
])
async def test_concurrent_moderation_audit_follows_committed_predecessor(database, monkeypatch, first, second):
    review_id = await seed_review(database)
    service = MasterReviewService()
    monkeypatch.setattr(service, "admin_review_detail", stub_detail)
    ready = asyncio.Barrier(2)
    read_barrier = asyncio.Barrier(2)
    first_read = asyncio.Event()
    release_first = asyncio.Event()
    first_committed = asyncio.Event()
    mode_known = asyncio.Event()
    state = {"locked": None, "pids": {}, "blocked": False}

    async def attempt(label, target):
        async with database() as session:
            # Preloading proves that FOR UPDATE must refresh an identity map too.
            cached = await session.get(MasterReview, review_id)
            assert cached.status == MasterReviewStatus.pending
            state["pids"][label] = await session.scalar(text("SELECT pg_backend_pid()"))
            original_get = session.get

            async def get(model, identity, **kwargs):
                if model is not MasterReview:
                    return await original_get(model, identity, **kwargs)
                locked = bool(kwargs.get("with_for_update"))
                if state["locked"] is None:
                    state["locked"] = locked
                    mode_known.set()
                assert state["locked"] == locked
                if label == "second":
                    await asyncio.wait_for(first_read.wait(), 8)
                result = await original_get(model, identity, **kwargs)
                if locked:
                    if label == "first":
                        first_read.set()
                        await asyncio.wait_for(release_first.wait(), 8)
                else:
                    if label == "first":
                        first_read.set()
                    await asyncio.wait_for(read_barrier.wait(), 8)
                    if label == "second":
                        await asyncio.wait_for(first_committed.wait(), 8)
                return result

            monkeypatch.setattr(session, "get", get)
            await asyncio.wait_for(ready.wait(), 8)
            result = await service.moderate(session, review_id, new_status=target, actor_id=1, reason="  reason  ")
            if label == "first":
                first_committed.set()
            assert await session.scalar(text("SELECT 1")) == 1
            return result

    async def observe():
        await asyncio.wait_for(mode_known.wait(), 8)
        if not state["locked"]:
            return
        await asyncio.wait_for(first_read.wait(), 8)
        async with database() as observer:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                blocked = await observer.scalar(text(
                    "SELECT :holder = ANY(pg_blocking_pids(:waiter))"
                ), {"holder": state["pids"]["first"], "waiter": state["pids"]["second"]})
                if blocked:
                    state["blocked"] = True
                    release_first.set()
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("Second moderator did not wait on first transaction")

    tasks = [asyncio.create_task(attempt("first", first)), asyncio.create_task(attempt("second", second)),
             asyncio.create_task(observe())]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks), 12)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(set(state["pids"].values())) == 2
    assert results[:2] == [review_id, review_id]
    if state["locked"]:
        assert state["blocked"]
    async with database() as session:
        review = await session.get(MasterReview, review_id)
        audit = list((await session.scalars(select(MasterReviewModerationAudit).order_by(
            MasterReviewModerationAudit.id))).all())
        assert [(a.from_status, a.to_status) for a in audit] == [
            (MasterReviewStatus.pending, first), (first, second)]
        assert [a.reason for a in audit] == ["reason", "reason"]
        assert [a.actor_id for a in audit] == [1, 1]
        assert review.status == second
        assert (review.published_at is not None) == (second == MasterReviewStatus.approved)
        assert review.moderated_by == 1


async def test_moderation_missing_and_invalid_action_remain_controlled(database, monkeypatch):
    service = MasterReviewService()
    monkeypatch.setattr(service, "admin_review_detail", stub_detail)
    async with database() as session:
        for target, code in [(MasterReviewStatus.pending, 422), (MasterReviewStatus.approved, 404)]:
            with pytest.raises(HTTPException) as error:
                await service.moderate(session, 999999, new_status=target, actor_id=1, reason=None)
            assert error.value.status_code == code
        assert list((await session.scalars(select(MasterReviewModerationAudit))).all()) == []
