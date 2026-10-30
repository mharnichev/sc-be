"""Pure privacy/schema checks; no provider or database."""
from datetime import UTC, datetime
import logging
from uuid import uuid4
import pytest
import sentry_sdk
from sentry_sdk.transport import Transport
from app.core import sentry as reporting
from app.schemas.master_rewards import SettlementCommand


@pytest.mark.parametrize('event',[
    {'transaction':'POST /api/v1/backoffice/master-rewards/entries','extra':{'private':'148573'}},
    {'exception':{'values':[{'stacktrace':{'frames':[{'module':'app.services.master_rewards'}]},'value':'private 148573'}]}},
    {'breadcrumbs':{'values':[{'data':{'query':'INSERT INTO master_reward_entries VALUES (148573)'}}]}},
    {'tags':{'rate_bps':148573}},
])
def test_private_events_and_transactions_are_discarded(event):
    assert reporting.before_send(event,{}) is None
    assert reporting.before_send_transaction(event,{}) is None


def test_private_sql_and_http_breadcrumbs_are_discarded():
    assert reporting.before_breadcrumb({'data':{'query':'SELECT * FROM master_reward_audits'}},{}) is None
    assert reporting.before_breadcrumb({'data':{'url':'https://example.test/api/v1/backoffice/master-rewards/entries'}},{}) is None
    assert reporting.traces_sampler({'asgi_scope':{'path':'/api/v1/backoffice/master-rewards/entries','method':'POST'}})==0


def test_real_sdk_never_exports_reward_events_or_breadcrumbs(monkeypatch):
    captured=[]
    class Memory(Transport):
        def capture_envelope(self,envelope):
            captured.extend(item.get_event() for item in envelope.items if item.type=='event')
    guard=sentry_sdk.init(**{**reporting.sentry_options(),'dsn':'https://fixture@example.invalid/1','transport':Memory})
    try:
        sentry_sdk.add_breadcrumb(category='sql',data={'query':'INSERT INTO master_reward_entries VALUES (148573)'})
        sentry_sdk.capture_event({'message':'private 148573','transaction':'POST /api/v1/backoffice/master-rewards/settlements'})
        sentry_sdk.capture_event({'message':'private 148573','logger':'app.services.master_rewards'})
        # Independent ordinary technical errors still work, without private crumbs.
        sentry_sdk.capture_event({'message':'synthetic unrelated health error '+uuid4().hex})
        sentry_sdk.flush()
        assert len(captured)==1
        assert '148573' not in repr(captured)
        assert all('master_reward_' not in str(crumb.get('data', {})) for crumb in captured[0].get('breadcrumbs', {}).get('values', []))
    finally:
        guard.__exit__(None,None,None)


def test_paid_timestamp_needs_timezone():
    with pytest.raises(ValueError):
        SettlementCommand(master_id=1,entries=[{'id':1,'expected_revision':1}],expected_total_minor=1,
            paid_at='2026-01-01T00:00:00',reason='manual',manual_payment_confirmed=True)
