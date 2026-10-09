"""Admission failures occur before any provider request and remain retryable."""
from datetime import timedelta

import pytest

from app.core.config import settings
from app.models.sms_queue import SmsQueueJob
from app.services.sms_queue import SmsQueueService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_sms_queue_integration import SimulatedClock, sms_payload

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("committed", [False, True], ids=["pre-admission", "committed-admission-ack-failed"])
async def test_admission_error_does_not_make_unsent_job_uncertain(database, committed):
    clock = SimulatedClock()
    provider_calls = []

    async def transport(job):
        provider_calls.append(job.id)
        return {"success_request": {"info": {"synthetic-admission-retry": "380500000001"}}}

    queue = SmsQueueService(session_factory=database, clock=clock, transport=transport)
    job = await queue.enqueue("send", sms_payload(1), idempotency_key="synthetic-admission-failure",
                              expires_at=clock.value + timedelta(hours=1))
    admit = queue._admit_transport

    async def fail_admission(claimed):
        if committed:
            assert await admit(claimed) is None
        raise OSError("Synthetic admission acknowledgement failure")

    queue._admit_transport = fail_admission
    assert await queue.process_one() is True
    assert provider_calls == []
    async with database() as session:
        persisted = await session.get(SmsQueueJob, job.id)
        assert persisted.status == "queued" and persisted.error_code == "admission_unavailable"
        assert persisted.attempts == 0 and persisted.provider_message_id is None
        assert (persisted.transport_started_at is not None) is committed
    queue._admit_transport = admit
    clock.advance(settings.sms_queue_retry_base_seconds + 2)
    assert await queue.process_one() is True
    assert provider_calls == [job.id]
    async with database() as session:
        persisted = await session.get(SmsQueueJob, job.id)
        assert persisted.status == "accepted" and persisted.error_code is None
        assert persisted.attempts == 1 and persisted.provider_message_id == "synthetic-admission-retry"
    assert await queue.process_one() is False
    assert provider_calls == [job.id]
