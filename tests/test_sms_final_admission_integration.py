"""Expiry at the durable transport admission boundary, with synthetic transport."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.sms_queue import SmsQueueJob
from app.services.sms_queue import SmsDispatchDecision, SmsQueueService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_sms_queue_integration import SimulatedClock, sms_payload, sms_transport  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("advance,expired", [(1, False), (2, True), (3, True)])
async def test_job_expiry_is_rechecked_after_slow_eligibility_before_transport(
    database, sms_transport, advance, expired,
):
    clock = SimulatedClock()
    eligible = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed_eligibility(_job):
        eligible.set()
        await asyncio.wait_for(proceed.wait(), 5)
        return SmsDispatchDecision()

    queue = SmsQueueService(session_factory=database, clock=clock, before_dispatch=delayed_eligibility)
    job = await queue.enqueue("send", sms_payload(1), idempotency_key="final-expiry",
                              expires_at=clock.value + timedelta(seconds=2))
    task = asyncio.create_task(queue.process_one())
    try:
        await asyncio.wait_for(eligible.wait(), 5)
        clock.advance(advance)
        proceed.set()
        assert await asyncio.wait_for(task, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(sms_transport.requests) == (0 if expired else 1)
    async with database() as session:
        persisted = await session.get(SmsQueueJob, job.id)
        if expired:
            assert persisted.status in {"skipped", "cancelled"}
            assert persisted.error_code == "expired"
            assert persisted.transport_started_at is None
        else:
            assert persisted.status == "accepted"
            assert persisted.transport_started_at == clock.value
        assert persisted.attempts == (0 if expired else 1)
        assert len(list((await session.scalars(select(SmsQueueJob))).all())) == 1
    clock.advance(3600)
    await queue.process_one()
    assert len(sms_transport.requests) == (0 if expired else 1)
