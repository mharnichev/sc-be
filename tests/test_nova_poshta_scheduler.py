"""Refresh lifecycle: gated startup, retry after failure and cancellation on shutdown."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main
from app.services import nova_poshta_directory as directory
from tests.test_lifespan_scheduler_contract import (
    FLAGS, anyio_backend, schedulers, shutdown_trace, startup_trace,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("enabled,key", [(False, None), (False, "key"), (True, None), (True, "key")])
async def test_directory_scheduler_requires_enabled_and_key(monkeypatch, schedulers, enabled, key):
    for flag in FLAGS:
        setattr(schedulers.settings, flag, False)
    schedulers.settings.nova_poshta_directory_enabled = enabled
    schedulers.settings.nova_poshta_api_key = key
    monkeypatch.setattr(main, "run_nova_poshta_directory_scheduler", schedulers.factory("run_nova_poshta_directory_scheduler"))
    specs = [(None, "run_customer_activity_notification_scheduler", "customer-activity-notification-scheduler", False)]
    if enabled and key:
        specs.append((None, "run_nova_poshta_directory_scheduler", "nova-poshta-directory-scheduler", False))
    async with main.lifespan(None):
        assert schedulers.events == startup_trace(specs)
    assert schedulers.events == startup_trace(specs) + shutdown_trace(specs)


async def test_refresh_runs_at_startup_retries_failure_and_honors_cancel(monkeypatch):
    events = []
    service = SimpleNamespace(refresh=AsyncMock(side_effect=[RuntimeError("provider offline"), True]))

    class Session:
        async def __aenter__(self):
            events.append("session")
            return self

        async def __aexit__(self, *args):
            events.append("close")

    async def sleep(seconds):
        events.append(seconds)
        if service.refresh.await_count == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(directory, "NovaPoshtaDirectory", lambda: service)
    monkeypatch.setattr(directory, "AsyncSessionLocal", Session)
    monkeypatch.setattr(directory, "asyncio", SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(directory, "settings", SimpleNamespace(nova_poshta_directory_poll_seconds=3600))
    with pytest.raises(asyncio.CancelledError):
        await directory.run_nova_poshta_directory_scheduler()
    assert service.refresh.await_count == 2
    assert events == ["session", "close", 3600, "session", "close", 3600]
