"""Bounded durable retry polling and configured-only application lifecycle."""
import asyncio
from types import SimpleNamespace

import pytest

from app import main
from app.services import brevo_scheduler
from tests.test_lifespan_scheduler_contract import (
    FLAGS, anyio_backend, schedulers, shutdown_trace, startup_trace,
)


pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("configured,first_failure", [(False, False), (True, False), (True, True)])
async def test_retry_poll_is_bounded_independent_and_survives_one_failed_iteration(
    monkeypatch, configured, first_failure,
):
    sleeps = []
    calls = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    async def sync_pending(*, limit):
        calls.append(limit)
        if first_failure and len(calls) == 1:
            raise RuntimeError("Synthetic retry storage failure")

    monkeypatch.setattr(brevo_scheduler, "asyncio", SimpleNamespace(
        sleep=sleep, CancelledError=asyncio.CancelledError,
    ))
    monkeypatch.setattr(brevo_scheduler, "brevo_contact_sync_service", SimpleNamespace(
        is_configured=lambda: configured, sync_pending=sync_pending,
    ))
    with pytest.raises(asyncio.CancelledError):
        await brevo_scheduler.run_brevo_sync_scheduler()
    assert sleeps == [30, 30, 30]
    assert calls == ([20, 20] if configured else [])


async def test_retry_worker_cancellation_is_propagated_without_another_poll(monkeypatch):
    events = []

    async def sleep(seconds):
        events.append(("sleep", seconds))

    async def sync_pending(*, limit):
        events.append(("sync", limit))
        raise asyncio.CancelledError

    monkeypatch.setattr(brevo_scheduler, "asyncio", SimpleNamespace(
        sleep=sleep, CancelledError=asyncio.CancelledError,
    ))
    monkeypatch.setattr(brevo_scheduler, "brevo_contact_sync_service", SimpleNamespace(
        is_configured=lambda: True, sync_pending=sync_pending,
    ))
    with pytest.raises(asyncio.CancelledError):
        await brevo_scheduler.run_brevo_sync_scheduler()
    assert events == [("sleep", 30), ("sync", 20)]


@pytest.mark.parametrize("enabled,key", [(False, "synthetic"), (True, None), (True, "synthetic")])
async def test_lifespan_starts_brevo_only_when_configured_and_cancels_it_last(schedulers, monkeypatch, enabled, key):
    for flag in FLAGS:
        setattr(schedulers.settings, flag, False)
    schedulers.settings.brevo_sync_enabled = enabled
    schedulers.settings.brevo_api_key = key
    monkeypatch.setattr(main, "run_brevo_sync_scheduler", schedulers.factory("run_brevo_sync_scheduler"))
    expected = [(None, "run_customer_activity_notification_scheduler", "customer-activity-notification-scheduler", False)]
    if enabled and key:
        expected.append((None, "run_brevo_sync_scheduler", "brevo-contact-sync-scheduler", False))
    async with main.lifespan(None):
        assert schedulers.events == startup_trace(expected)
        assert [task.name for task in schedulers.tasks] == [spec[2] for spec in expected]
    assert schedulers.events == startup_trace(expected) + shutdown_trace(expected)
    assert all(task.cancelled and task.awaited for task in schedulers.tasks)
