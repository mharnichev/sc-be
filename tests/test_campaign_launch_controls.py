from datetime import UTC, datetime
from types import SimpleNamespace
import asyncio

import pytest
from sqlalchemy.dialects import postgresql

from app.services.campaign_dispatch import sending_interval
from app.services.campaign_runs import delivery_options, marketing_contact_predicate, sms_parts
from app.services.campaign_runtime import worker_healthy, worker_iteration_succeeded


@pytest.mark.parametrize('now,expected', [
    (datetime(2026, 9, 7, 7, tzinfo=UTC), datetime(2026, 9, 7, 7, tzinfo=UTC)),
    (datetime(2026, 9, 7, 15, tzinfo=UTC), datetime(2026, 9, 8, 7, tzinfo=UTC)),
    (datetime(2026, 3, 29, 6, tzinfo=UTC), datetime(2026, 3, 29, 7, tzinfo=UTC)),
    (datetime(2026, 10, 25, 6, tzinfo=UTC), datetime(2026, 10, 25, 8, tzinfo=UTC)),
])
def test_daytime_window_boundaries_and_dst(now, expected):
    window = {'start': '10:00', 'end': '18:00', 'days': list(range(7))}
    assert sending_interval(now, window)[0] == expected


def test_typed_controls_override_legacy_metadata_and_nulls_fall_back():
    campaign = SimpleNamespace(metadata_json={'sms_recipients_per_minute': 60, 'marketing_frequency_days': 7},
                               sms_recipients_per_minute=20, marketing_frequency_days=None,
                               marketing_max_contacts=2, marketing_cap_days=30)
    result = delivery_options(campaign)
    assert result['sms_recipients_per_minute'] == 20
    assert result['marketing_frequency_days'] == 7
    assert result['marketing_max_contacts'] == 2


@pytest.mark.parametrize('body,parts', [('a' * 160, 1), ('a' * 161, 2), ('^' * 81, 2), ('Я' * 70, 1), ('Я' * 71, 2)])
def test_personalized_sms_part_estimate(body, parts):
    assert sms_parts(body) == parts


def test_marketing_cap_counts_accepted_or_reserved_contacts_and_excludes_self():
    query = marketing_contact_predicate(9, datetime(2026, 10, 5, tzinfo=UTC), 7,
                                      exclude_recipient_id=3, max_contacts=2, cap_days=30)
    compiled = query.compile(dialect=postgresql.dialect())
    assert 'count(message_recipients.id)' in str(compiled)
    assert 'sent_at' in str(compiled) and 'send_started_at' in str(compiled)
    assert 2 in compiled.params.values() and 3 in compiled.params.values()


def test_runtime_probe_requires_successful_live_worker_and_expires(monkeypatch):
    async def run():
        event = asyncio.Event()
        task = asyncio.create_task(event.wait(), name='sandbox-campaign-worker')
        try:
            assert not worker_healthy('sandbox-campaign-worker', 60)
            worker_iteration_succeeded('sandbox-campaign-worker')
            assert worker_healthy('sandbox-campaign-worker', 60)
            monkeypatch.setattr('app.services.campaign_runtime.time.monotonic', lambda: float('inf'))
            assert not worker_healthy('sandbox-campaign-worker', 60)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_future_offer_launch_defaults_to_start_and_rejects_invalid_explicit_schedule():
    from app.services.campaign_runs import offer_schedule
    from fastapi import HTTPException
    from datetime import timedelta
    now = datetime(2026, 10, 5, 10, tzinfo=UTC)
    starts = now + timedelta(days=1)
    expires = starts + timedelta(days=14)
    campaign = SimpleNamespace(offer_master_id=1, offer_starts_at=starts, offer_expires_at=expires)
    assert offer_schedule(campaign, None, now) == starts
    assert offer_schedule(campaign, starts, now) == starts
    for invalid in (now, expires):
        with pytest.raises(HTTPException) as exc:
            offer_schedule(campaign, invalid, now)
        assert exc.value.detail['code'] == 'launch_outside_offer_validity'


@pytest.mark.parametrize('change', [{'type': 'birthday'}, {'channel_strategy': 'sms_then_telegram'}, {'metadata_json': {'recipient': 'master'}}])
def test_offer_configuration_rejects_nonmarketing_or_mixed_target_paths(change):
    from app.services.campaign_runs import CampaignRunService
    from fastapi import HTTPException
    payload = {'name': 'Offer', 'offer_master_id': 1, 'type': 'manual', 'channel': 'sms', 'purpose': 'marketing'}
    payload.update(change)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(CampaignRunService().prepare_campaign_data(None, payload))
    assert exc.value.status_code == 422
