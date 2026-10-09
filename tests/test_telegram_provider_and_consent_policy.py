import asyncio
import threading

import pytest

from app.models.messaging import ClientCommunicationPreference, ConsentStatus, MessagePurpose
from app.services.messaging import MessagingService, TelegramMessageProvider


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_cancelled_provider_waits_for_live_transport_thread_to_finish():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    def transport():
        started.set()
        assert release.wait(5)
        finished.set()
        return {"ok": True}
    task = asyncio.create_task(TelegramMessageProvider()._drained_request(transport))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_guests_can_receive_transactional_messages_but_marketing_requires_consent():
    service = MessagingService()
    assert service.communication_allowed(None, MessagePurpose.transactional) == (True, None)
    assert service.communication_allowed(None, MessagePurpose.marketing)[0] is False
    assert service.has_marketing_consent(None) is False


@pytest.mark.parametrize("preference", [
    ClientCommunicationPreference(customer_id=1, marketing_consent=ConsentStatus.opted_out),
    ClientCommunicationPreference(customer_id=1, marketing_consent=ConsentStatus.unknown),
    ClientCommunicationPreference(customer_id=1, marketing_consent=ConsentStatus.opted_in, do_not_contact=True),
])
def test_manual_marketing_policy_never_overrides_consent_or_do_not_contact(preference):
    assert MessagingService().communication_allowed(preference, MessagePurpose.marketing)[0] is False
