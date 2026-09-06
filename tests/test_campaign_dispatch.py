from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.models.messaging import CampaignStatus, CampaignType
from app.schemas.messaging import CampaignCreate, CampaignSendingWindow
from app.services.campaign_dispatch import CampaignDispatchService, estimated_finish, sending_interval
from app.services.campaign_runs import delivery_options


def test_sending_window_end_is_exclusive_and_skips_weekend():
    # Friday20:00Kyiv is outside9-20; next start is Monday09:00Kyiv.
    now = datetime(2026, 9, 11, 17, tzinfo=UTC)
    start, end = sending_interval(now, {"start": "09:00", "end": "20:00", "days": [0, 1, 2, 3, 4]})
    assert start == datetime(2026, 9, 14, 6, tzinfo=UTC)
    assert end == datetime(2026, 9, 14, 17, tzinfo=UTC)


def test_overnight_window_belongs_to_opening_weekday():
    # Tuesday01:00Kyiv remains inside Monday's22:00-02:00window.
    now = datetime(2026, 9, 7, 22, tzinfo=UTC)
    window = {"start": "22:00", "end": "02:00", "days": [0]}
    assert sending_interval(now, window) == (now, datetime(2026, 9, 7, 23, tzinfo=UTC))
    next_start, _ = sending_interval(datetime(2026, 9, 7, 23, tzinfo=UTC), window)
    assert next_start == datetime(2026, 9, 14, 19, tzinfo=UTC)


def test_eta_consumes_only_open_sending_time_across_weekend():
    now = datetime(2026, 9, 11, 16, tzinfo=UTC)  # Friday19:00Kyiv
    finish = estimated_finish(now, 7200, {"start": "09:00", "end": "20:00", "days": [0, 1, 2, 3, 4]})
    assert finish == datetime(2026, 9, 14, 7, tzinfo=UTC)  # Monday10:00Kyiv


def test_dst_gap_normalizes_forward_and_repeated_hour_uses_earlier_occurrence():
    window = {"start": "03:00", "end": "05:00", "days": [6]}
    start, end = sending_interval(datetime(2026, 3, 29, 0, tzinfo=UTC), window)
    assert start == datetime(2026, 3, 29, 1, tzinfo=UTC)  # nonexistent03:00 ->04:00Kyiv
    assert end == datetime(2026, 3, 29, 2, tzinfo=UTC)
    start, end = sending_interval(datetime(2026, 10, 24, 23, tzinfo=UTC), {"start": "03:00", "end": "04:00", "days": [6]})
    assert start == datetime(2026, 10, 25, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 25, 2, tzinfo=UTC)


@pytest.mark.parametrize("window", [
    {"start": "9:00", "end": "20:00"},
    {"start": "09:00", "end": "09:00"},
    {"start": "09:00", "end": "20:00", "days": []},
    {"start": "09:00", "end": "20:00", "days": [7]},
    {"start": "09:00", "end": "20:00", "timezone": "UTC"},
])
def test_window_schema_rejects_ambiguous_or_unsupported_schedules(window):
    with pytest.raises(ValidationError):
        CampaignSendingWindow.model_validate(window)


def test_configured_campaign_default_is_shared_by_schema_and_legacy_campaigns(monkeypatch):
    monkeypatch.setattr(settings, "sms_campaign_recipients_per_minute", 12)
    assert CampaignCreate(name="Campaign", type=CampaignType.manual).sms_recipients_per_minute == 12
    assert delivery_options(SimpleNamespace(metadata_json={}))['sms_recipients_per_minute'] == 12


class QueryResult:
    def __init__(self, value):
        self.value = value
    def all(self):
        return self.value
    def one(self):
        return self.value
    def scalar_one(self):
        return self.value


class ProgressSession:
    def __init__(self, results, account):
        self.results = iter(results)
        self.account = account
    async def execute(self, _):
        return QueryResult(next(self.results))
    async def get(self, *_):
        return self.account


def test_eta_respects_slower_account_cap_cooldown_and_existing_campaign_use(monkeypatch):
    monkeypatch.setattr(settings, "sms_club_requests_per_second", 0.1)
    now = datetime(2026, 9, 7, 10, tzinfo=UTC)
    account = SimpleNamespace(cooldown_until=now + timedelta(seconds=120), next_request_at=None)
    # 12ownrecipients at60/min but account6/min and20queuedrequests overall.
    session = ProgressSession([[("queued", 12)], 0, now + timedelta(seconds=30), (60, now), 20], account)
    campaign = SimpleNamespace(id=1, status=CampaignStatus.active, metadata_json={})
    run = SimpleNamespace(id=2, status="snapshotted", campaign_snapshot={"sms_recipients_per_minute": 60, "sending_window": None})
    result = asyncio.run(CampaignDispatchService(clock=lambda: now).progress(session, campaign, run=run))
    assert result["estimated_remaining_seconds"] == 320  #120cooldown+20/0.1knownrequests
    assert result["estimated_completion_at"] == now + timedelta(seconds=320)


def test_empty_queue_does_not_report_waiting_for_next_window():
    now = datetime(2026, 9, 7, 21, tzinfo=UTC)
    session = ProgressSession([[("accepted", 3)], 0], None)
    campaign = SimpleNamespace(id=1, status=CampaignStatus.active, metadata_json={})
    run = SimpleNamespace(id=2, status="completed", campaign_snapshot={
        "sms_recipients_per_minute": 60, "sending_window": {"start": "09:00", "end": "20:00", "days": list(range(7))},
    })
    result = asyncio.run(CampaignDispatchService(clock=lambda: now).progress(session, campaign, run=run))
    assert result["estimated_remaining_seconds"] == 0
    assert result["next_window_at"] is None
