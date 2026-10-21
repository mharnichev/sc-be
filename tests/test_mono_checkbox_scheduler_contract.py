"""Explicit opt-in registration without creating real worker coroutines."""
import pytest
from app import main
from tests.test_lifespan_scheduler_contract import schedulers, FLAGS


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
async def test_mono_worker_is_opt_in_and_drained_with_existing_owner(schedulers, monkeypatch, enabled):
    for flag in FLAGS:
        setattr(schedulers.settings, flag, False)
    schedulers.settings.customer_activity_notification_scheduler_enabled = False
    schedulers.settings.mono_checkbox_auto_enabled = enabled
    monkeypatch.setattr(main, "run_mono_checkbox_scheduler", schedulers.factory("mono"))
    async with main.lifespan(None):
        expected = [("factory", "mono"), ("create", "mono-checkbox-payment-receipt-scheduler")] if enabled else []
        assert schedulers.events == expected
    if enabled:
        assert schedulers.events[-2:] == [("cancel", "mono-checkbox-payment-receipt-scheduler"), ("await", "mono-checkbox-payment-receipt-scheduler")]
    else:
        assert schedulers.events == []
