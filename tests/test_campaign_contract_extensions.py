import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.models.messaging import MessageChannel
from app.services.campaign_runs import CampaignRunService, choose_channel
from app.services.campaign_analytics import offer_analytics


def test_offer_allows_telegram_first_and_preserves_typed_delivery_configuration():
    result = asyncio.run(CampaignRunService().prepare_campaign_data(None, {
        'name': 'Other master', 'offer_master_id': 9, 'type': 'manual',
        'channel': 'sms', 'purpose': 'marketing', 'channel_strategy': 'telegram_then_sms',
        'sms_recipients_per_minute': 20,
    }))
    assert result['metadata_json']['channel_strategy'] == 'telegram_then_sms'
    assert result['sms_recipients_per_minute'] == 20
    assert result['status'] == 'draft'
    assert result['sending_window']['start'] == '10:00'


def test_telegram_first_falls_back_only_for_unreachable_destination():
    customer = SimpleNamespace(phone='+380501111111')
    preference = SimpleNamespace(telegram_chat_id='sandbox-chat')
    assert choose_channel(customer, preference, 'telegram_then_sms', 'sms') == (MessageChannel.telegram, None)
    assert choose_channel(customer, None, 'telegram_then_sms', 'sms') == (MessageChannel.sms, None)
    assert choose_channel(SimpleNamespace(phone=None), None, 'telegram_then_sms', 'sms') == (None, 'channel_unreachable')


@pytest.mark.parametrize('status,response,expected', [
    (403, {'ok': False, 'description': 'Forbidden: bot was blocked by the user'}, True),
    (400, {'ok': False, 'description': 'Bad Request: chat not found'}, True),
    (403, {'ok': False, 'description': 'Forbidden: user is deactivated'}, True),
    (500, {'ok': False, 'description': 'Internal server error'}, False),
    (429, {'ok': False, 'description': 'Too Many Requests'}, False),
    (200, {'ok': True, 'result': {'message_id': 1}}, False),
    (403, {'ok': True, 'description': 'bot was blocked'}, False),
    (403, None, False),
])
def test_fallback_requires_definitive_nonacceptance(status, response, expected):
    from app.services.messaging import telegram_destination_unavailable
    assert telegram_destination_unavailable(status, response) is expected


def test_analytics_period_applies_same_cohort_to_every_metric_source():
    class Rows:
        def __init__(self, rows): self.rows = rows
        def all(self): return self.rows
        def one(self): return self.rows

    class Session:
        def __init__(self): self.statements = []; self.execution = 0
        async def scalar(self, stmt): self.statements.append(stmt); return 0
        async def execute(self, stmt):
            self.statements.append(stmt)
            self.execution += 1
            return Rows({1: [], 2: (0, 0, 0), 3: [], 4: (0, 0, 0, 0)}[self.execution])

    session = Session()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    result = asyncio.run(offer_analytics(session, date_from=start, date_to=end))
    assert result['period_basis'] == 'run_cohort'
    assert result['date_from'] == start and result['date_to'] == end
    assert result['booking_conversion_percent'] is None
    for statement in session.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        assert 'coalesce(campaign_runs.evaluated_at, campaign_runs.created_at) >=' in str(compiled)
        assert 'coalesce(campaign_runs.evaluated_at, campaign_runs.created_at) <' in str(compiled)
        assert start in compiled.params.values() and end in compiled.params.values()


def test_analytics_rejects_reversed_period():
    from app.api.v1.routes.campaign_analytics import analytics
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(HTTPException) as cause:
        asyncio.run(analytics(date_from=start, date_to=start, session=None))
    assert cause.value.status_code == 422
