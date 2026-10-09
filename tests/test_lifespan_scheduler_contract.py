"""Exact scheduler lifespan traces with inert factories and awaitable task doubles."""
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app import main


# Order, names, and gates are observable lifecycle contracts.
SCHEDULERS = (
    ("sms_queue_worker_enabled", "run_sms_queue_worker", "sms-queue-worker", True),
    ("campaign_run_scheduler_enabled", "run_campaign_scheduler", "campaign-run-scheduler", False),
    ("product_top_scheduler_enabled", "run_product_popularity_scheduler", "product-top-cache-scheduler", False),
    ("service_popularity_scheduler_enabled", "run_service_popularity_scheduler", "service-popularity-cache-scheduler", False),
    ("review_request_scheduler_enabled", "run_review_request_scheduler", "review-request-scheduler", False),
    ("sms_delivery_status_scheduler_enabled", "run_sms_delivery_status_scheduler", "sms-delivery-status-scheduler", True),
    ("booking_funnel_digest_scheduler_enabled", "run_booking_funnel_digest_scheduler", "booking-funnel-weekly-digest-scheduler", False),
    ("waitlist_offer_scheduler_enabled", "run_waitlist_offer_scheduler", "waitlist-offer-scheduler", False),
    (None, "run_customer_activity_notification_scheduler", "customer-activity-notification-scheduler", False),
    ("repeat_booking_scheduler_enabled", "run_repeat_booking_scheduler", "repeat-booking-scheduler", False),
    ("master_schedule_reminder_scheduler_enabled", "run_master_schedule_reminder_scheduler", "master-schedule-reminder-scheduler", False),
)
FLAGS = tuple(flag for flag, _, _, _ in SCHEDULERS if flag is not None)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass(frozen=True)
class FactoryToken:
    factory_name: str


class FakeTask:
    def __init__(self, harness, name):
        self.harness = harness
        self.name = name
        self.cancelled = False
        self.awaited = False
        self.disposed = False

    def cancel(self):
        self.harness.events.append(("cancel", self.name))
        self.cancelled = True

    def __await__(self):
        self.harness.events.append(("await", self.name))
        self.awaited = True
        error = self.harness.shutdown_errors.get(self.name)
        if error is not None:
            raise error
        if self.harness.cancelled_on_await:
            raise asyncio.CancelledError
        yield from ()
        return None


class SchedulerHarness:
    def __init__(self):
        self.events = []
        self.tasks = []
        self.shutdown_errors = {}
        self.cancelled_on_await = True
        self.startup_error = RuntimeError("synthetic startup failure")
        self.fail_factory = None
        self.fail_create = None
        self.settings = SimpleNamespace(sms_provider="smsclub", **dict.fromkeys(FLAGS, True))

    def factory(self, factory_name):
        def invoke():
            self.events.append(("factory", factory_name))
            if factory_name == self.fail_factory:
                raise self.startup_error
            return FactoryToken(factory_name)
        return invoke

    def create_task(self, token, *, name):
        assert isinstance(token, FactoryToken)
        self.events.append(("create", name))
        if name == self.fail_create:
            raise self.startup_error
        task = FakeTask(self, name)
        self.tasks.append(task)
        return task


@pytest.fixture
def schedulers(monkeypatch):
    harness = SchedulerHarness()
    monkeypatch.setattr(main, "settings", harness.settings)
    # Replace only the main module's reference, preserving pytest's actual asyncio.
    monkeypatch.setattr(main, "asyncio", SimpleNamespace(
        create_task=harness.create_task, CancelledError=asyncio.CancelledError, Task=asyncio.Task,
    ))
    for _, factory_name, _, _ in SCHEDULERS:
        monkeypatch.setattr(main, factory_name, harness.factory(factory_name))
    yield harness
    # Doubles contain no coroutines or real tasks, even after an aborted shutdown.
    for task in harness.tasks:
        assert isinstance(task, FakeTask)
        assert not isinstance(task, asyncio.Task)
        task.disposed = True
    assert all(task.disposed for task in harness.tasks)


def enabled_specs(settings):
    return [spec for spec in SCHEDULERS
            if (spec[0] is None or getattr(settings, spec[0]))
            and (not spec[3] or settings.sms_provider == "smsclub")]


def startup_trace(specs):
    return [event for _, factory, name, _ in specs for event in (("factory", factory), ("create", name))]


def shutdown_trace(specs):
    return [event for _, _, name, _ in specs for event in (("cancel", name), ("await", name))]


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["stub", "smsclub"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_all_scheduler_flags_preserve_factory_name_and_order(schedulers, provider, enabled):
    before = asyncio.all_tasks()
    schedulers.settings.sms_provider = provider
    for flag in FLAGS:
        setattr(schedulers.settings, flag, enabled)
    expected = enabled_specs(schedulers.settings)
    async with main.lifespan(None):
        assert schedulers.events == startup_trace(expected)
        assert [task.name for task in schedulers.tasks] == [spec[2] for spec in expected]
        if not enabled:
            assert [task.name for task in schedulers.tasks] == ["customer-activity-notification-scheduler"]
        schedulers.events.append(("body",))
    assert schedulers.events == startup_trace(expected) + [("body",)] + shutdown_trace(expected)
    assert all(task.cancelled and task.awaited for task in schedulers.tasks)
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["stub", "smsclub"])
@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("enabled", [False, True])
async def test_one_scheduler_flag_provider_matrix(schedulers, provider, flag, enabled):
    before = asyncio.all_tasks()
    schedulers.settings.sms_provider = provider
    for candidate in FLAGS:
        setattr(schedulers.settings, candidate, False)
    setattr(schedulers.settings, flag, enabled)
    expected = enabled_specs(schedulers.settings)
    async with main.lifespan(None):
        assert schedulers.events == startup_trace(expected)
    assert schedulers.events == startup_trace(expected) + shutdown_trace(expected)
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
@pytest.mark.parametrize("error_index", [0, 4, 8, 10])
async def test_shutdown_other_error_propagates_without_cancelling_later_tasks(schedulers, error_index):
    before = asyncio.all_tasks()
    error = RuntimeError("synthetic scheduler shutdown failure")
    schedulers.shutdown_errors[SCHEDULERS[error_index][2]] = error
    with pytest.raises(RuntimeError) as observed:
        async with main.lifespan(None):
            assert schedulers.events == startup_trace(SCHEDULERS)
    assert observed.value is error
    assert schedulers.events == startup_trace(SCHEDULERS) + shutdown_trace(SCHEDULERS[:error_index + 1])
    assert all(task.cancelled and task.awaited for task in schedulers.tasks[:error_index + 1])
    assert all(not task.cancelled and not task.awaited for task in schedulers.tasks[error_index + 1:])
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
async def test_already_finished_tasks_are_cancelled_and_awaited_sequentially(schedulers):
    before = asyncio.all_tasks()
    schedulers.cancelled_on_await = False
    async with main.lifespan(None):
        pass
    assert schedulers.events == startup_trace(SCHEDULERS) + shutdown_trace(SCHEDULERS)
    assert all(task.cancelled and task.awaited for task in schedulers.tasks)
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
@pytest.mark.parametrize("shutdown_failure", [False, True])
async def test_body_exception_keeps_existing_shutdown_precedence(schedulers, shutdown_failure):
    before = asyncio.all_tasks()
    body_error = RuntimeError("synthetic body failure")
    shutdown_error = RuntimeError("synthetic shutdown failure")
    if shutdown_failure:
        schedulers.shutdown_errors[SCHEDULERS[4][2]] = shutdown_error
    with pytest.raises(RuntimeError) as observed:
        async with main.lifespan(None):
            schedulers.events.append(("body-error",))
            raise body_error
    assert observed.value is (shutdown_error if shutdown_failure else body_error)
    stopped = SCHEDULERS[:5] if shutdown_failure else SCHEDULERS
    assert schedulers.events == startup_trace(SCHEDULERS) + [("body-error",)] + shutdown_trace(stopped)
    if shutdown_failure:
        assert all(not task.cancelled and not task.awaited for task in schedulers.tasks[5:])
    assert asyncio.all_tasks() == before


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ["factory", "create"])
async def test_startup_failure_remains_outside_shutdown_finally(schedulers, failure_stage):
    before = asyncio.all_tasks()
    failing_spec = SCHEDULERS[2]
    if failure_stage == "factory":
        schedulers.fail_factory = failing_spec[1]
    else:
        schedulers.fail_create = failing_spec[2]
    with pytest.raises(RuntimeError) as observed:
        async with main.lifespan(None):
            pytest.fail("Startup failure must prevent entry into the body")
    assert observed.value is schedulers.startup_error
    expected = startup_trace(SCHEDULERS[:2]) + [("factory", failing_spec[1])]
    if failure_stage == "create":
        expected.append(("create", failing_spec[2]))
    assert schedulers.events == expected
    assert all(not task.cancelled and not task.awaited for task in schedulers.tasks)
    assert asyncio.all_tasks() == before
