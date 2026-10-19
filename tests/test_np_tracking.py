"""Fixture-only tracking tests. No real TTN requests or order writes."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.services import np_tracking as tracking

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
NUMBER = '20450000000001'


class Session:
    def __init__(self):
        self.events = []

    async def scalar(self, query):
        params = query.compile().params
        if 'event_key_1' not in params:
            return next((e.normalized_status for e in reversed(self.events) if e.normalized_status != 'requires_review'), None)
        key = params['event_key_1']
        return next((i + 1 for i, event in enumerate(self.events) if event.event_key == key), None)

    def add(self, event):
        self.events.append(event)


def shipment(**kwargs):
    values = dict(id=10, tracking_number=NUMBER, simulation=False, carrier='nova_poshta',
        raw_status='1', tracking_status=None, provider_event_at=None, tracking_failures=0,
        return_of_shipment_id=None, last_checked_at=None, last_attempt_status='never')
    values.update(kwargs)
    return SimpleNamespace(**values)


def apply(session, item, code, when=NOW, now=NOW):
    asyncio.run(tracking.apply_observation(session, item, tracking.Observation(NUMBER, code, when), now))


@pytest.mark.parametrize(('code', 'status'), [
    ('1', 'preparing'), ('2', 'cancelled'), ('3', 'requires_review'),
    ('4', 'in_transit'), ('41', 'in_transit'), ('5', 'in_transit'), ('6', 'in_transit'),
    ('7', 'ready_for_pickup'), ('8', 'ready_for_pickup'), ('9', 'delivered'),
    ('10', 'delivered'), ('11', 'delivered'), ('12', 'handed_over'), ('15', 'in_transit'),
    ('101', 'in_transit'), ('102', 'returning'), ('103', 'refused'),
    ('104', 'requires_review'), ('105', 'requires_review'), ('106', 'delivered'),
    ('107', 'in_transit'), ('111', 'requires_review'), ('112', 'requires_review'),
    ('124', 'requires_review'), ('999', 'requires_review'),
])
def test_mapping(code, status):
    item, session = shipment(), Session()
    apply(session, item, code)
    assert item.tracking_status == status
    assert item.raw_status == code
    assert len(session.events) == 1


def test_idempotency_stale_and_explicit_return_after_receipt():
    item, session = shipment(), Session()
    for i, code in enumerate(['12', '5', '7', '9', '103', '102']):
        when = NOW + timedelta(hours=i)
        apply(session, item, code, when, when)
        apply(session, item, code, when, when + timedelta(seconds=1))
    assert len(session.events) == 6
    assert item.tracking_status == 'returning'
    apply(session, item, '5', NOW, NOW + timedelta(days=1))
    assert item.tracking_status == 'returning'
    assert item.last_attempt_status == 'stale_response'
    assert len(session.events) == 6


def test_regression_and_missing_time_do_not_erase_evidence():
    item, session = shipment(), Session()
    apply(session, item, '9')
    apply(session, item, '1', NOW + timedelta(hours=1))
    assert item.tracking_status == 'delivered'
    assert item.last_attempt_status == 'conflicting_response'
    apply(session, item, '102', None)
    assert item.tracking_status == 'delivered'
    assert item.provider_event_at == NOW
    assert len(session.events) == 1


def test_linked_return_leg_distinct_from_cod_reverse_document():
    session = Session()
    outgoing = shipment()
    apply(session, outgoing, '106')
    assert outgoing.tracking_status == 'delivered'
    returned = shipment(return_of_shipment_id=9)
    apply(session, returned, '7')
    assert returned.tracking_status == 'returning'
    apply(session, returned, '9', NOW + timedelta(hours=1))
    assert returned.tracking_status == 'returned'


def test_mocks_and_foreign_ttn_rejected():
    for item in [shipment(simulation=True), shipment(carrier='mock_nova_poshta'), shipment(tracking_number='20450000000002')]:
        with pytest.raises(tracking.TrackingError, match='shipment_mismatch'):
            apply(Session(), item, '9')


def test_failure_preserves_last_status_and_caps_backoff():
    item = shipment(tracking_status='ready_for_pickup', tracking_failures=100, last_checked_at=NOW)
    tracking.mark_failure(item, 'timeout', NOW)
    assert item.tracking_status == 'ready_for_pickup'
    assert item.last_checked_at == NOW
    assert item.last_attempt_status == 'timeout'
    assert item.next_poll_at == NOW + timedelta(seconds=max(
        tracking.settings.np_tracking_max_backoff_seconds, tracking.settings.np_tracking_cooldown_seconds))


def test_event_time_is_kyiv_scan_and_receipt_fallback_only():
    assert tracking.event_time({'DateScan': '2026-10-17 15:00:00'}) == NOW
    assert tracking.event_time({'DateScan': '0001-01-01 00:00:00', 'StatusCode': '9',
        'RecipientDateTime': '17.10.2026 15:00:00'}) == NOW
    assert tracking.event_time({'StatusCode': '5', 'RecipientDateTime': '17.10.2026 15:00:00'}) is None
    assert tracking.event_time({'TrackingUpdateDate': '2026-10-17 15:00:00'}) is None


def mock_client(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(tracking.httpx, 'AsyncClient', lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(tracking.settings, 'nova_poshta_api_key', 'FIXTURE-SECRET')


def test_adapter_request_and_safe_response(monkeypatch):
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body['modelName'] == 'TrackingDocumentGeneral'
        assert body['calledMethod'] == 'getStatusDocuments'
        assert body['methodProperties'] == {'Documents': [{'DocumentNumber': NUMBER}]}
        return httpx.Response(200, json={'success': True, 'data': [
            {'Number': NUMBER, 'StatusCode': '9', 'DateScan': '2026-10-17 15:00:00', 'RecipientFullName': 'PRIVATE'}]})
    mock_client(monkeypatch, handler)
    observations = asyncio.run(tracking.NovaPoshtaTracking().fetch([NUMBER]))
    assert observations[NUMBER] == tracking.Observation(NUMBER, '9', NOW)
    assert 'PRIVATE' not in repr(observations)


@pytest.mark.parametrize('data', [[], [{'Number': NUMBER, 'StatusCode': '9'}] * 2,
    [{'Number': '20450000000002', 'StatusCode': '9'}], [{'Number': NUMBER}],
    [{'Number': NUMBER, 'StatusCode': 'secret-sensitive-error'}]])
def test_invalid_batches_never_partially_apply(monkeypatch, data):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={'success': True, 'data': data}))
    with pytest.raises(tracking.TrackingError, match='^invalid_response$'):
        asyncio.run(tracking.NovaPoshtaTracking().fetch([NUMBER]))


@pytest.mark.parametrize(('failure', 'expected'), [('timeout', 'timeout'), ('429', 'rate_limited'),
    ('503', 'unavailable'), ('logical', 'invalid_response')])
def test_adapter_safe_failures(monkeypatch, failure, expected):
    def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('SECRET AND PRIVATE PAYLOAD', request=request)
        if failure == 'logical':
            return httpx.Response(200, json={'success': False, 'errors': ['SECRET AND PRIVATE PAYLOAD']})
        return httpx.Response(int(failure), text='SECRET AND PRIVATE PAYLOAD')
    mock_client(monkeypatch, handler)
    with pytest.raises(tracking.TrackingError) as error:
        asyncio.run(tracking.NovaPoshtaTracking().fetch([NUMBER]))
    assert str(error.value) == expected


def test_no_credentials_no_transport(monkeypatch):
    monkeypatch.setattr(tracking.settings, 'nova_poshta_api_key', None)
    with pytest.raises(tracking.TrackingError, match='not_configured'):
        asyncio.run(tracking.NovaPoshtaTracking().fetch([NUMBER]))


class PollSession:
    def __init__(self, *, acquired=True, next_request_at=None, items=()):
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        self.acquired = acquired
        self.state = SimpleNamespace(next_request_at=next_request_at)
        self.items = items
        self.statements = []

    async def scalar(self, query):
        self.statements.append(query)
        return self.acquired

    async def execute(self, query):
        self.statements.append(query)

    async def get(self, model, key, **kwargs):
        assert kwargs == {'with_for_update': True}
        return self.state

    async def scalars(self, query):
        self.statements.append(query)
        return SimpleNamespace(all=lambda: self.items)


def enable(monkeypatch):
    monkeypatch.setattr(tracking.settings, 'np_tracking_enabled', True)
    monkeypatch.setattr(tracking.settings, 'nova_poshta_api_key', 'FIXTURE-SECRET')


def test_competing_poll_and_durable_cooldown_do_not_call_provider(monkeypatch):
    enable(monkeypatch)
    async def forbidden(self, numbers):
        raise AssertionError('Provider should not be called')
    monkeypatch.setattr(tracking.NovaPoshtaTracking, 'fetch', forbidden)
    locked = PollSession(acquired=False)
    assert asyncio.run(tracking.refresh_due(locked)) == 0
    assert len(locked.statements) == 1
    cooldown = PollSession(next_request_at=datetime.now(UTC) + timedelta(hours=1))
    assert asyncio.run(tracking.refresh_due(cooldown)) == 0
    assert len(cooldown.statements) == 2


def test_poll_selection_is_real_due_bounded_locked_and_owner_scoped(monkeypatch):
    from sqlalchemy.dialects import postgresql
    enable(monkeypatch)
    session = PollSession()
    assert asyncio.run(tracking.refresh_due(session, order_id=123)) == 0
    query = session.statements[-1].compile(dialect=postgresql.dialect())
    sql = str(query)
    assert 'simulation IS false' in sql
    assert 'carrier =' in sql and 'nova_poshta' in query.params.values()
    assert 'next_poll_at IS NULL' in sql and 'next_poll_at <=' in sql
    assert 'order_id =' in sql and 123 in query.params.values()
    assert 'FOR UPDATE SKIP LOCKED' in sql
    assert 'LIMIT' in sql


def test_poll_failure_sets_retry_and_global_spacing(monkeypatch):
    enable(monkeypatch)
    item = shipment(tracking_status='ready_for_pickup', last_checked_at=NOW)
    session = PollSession(items=[item])
    async def unavailable(self, numbers):
        assert numbers == [NUMBER]
        raise tracking.TrackingError('rate_limited')
    monkeypatch.setattr(tracking.NovaPoshtaTracking, 'fetch', unavailable)
    before = datetime.now(UTC)
    assert asyncio.run(tracking.refresh_due(session)) == 1
    assert item.tracking_status == 'ready_for_pickup'
    assert item.last_checked_at == NOW
    assert item.last_attempt_status == 'rate_limited'
    assert item.next_poll_at > before
    assert session.state.next_request_at >= before + timedelta(seconds=tracking.settings.np_tracking_request_interval_seconds)


def test_invalid_saved_ttn_never_calls_provider(monkeypatch):
    enable(monkeypatch)
    item = shipment(tracking_number='MOCK-123')
    session = PollSession(items=[item])
    assert asyncio.run(tracking.refresh_due(session)) == 0
    assert item.last_attempt_status == 'invalid_tracking_number'


def test_review_cannot_reset_terminal_transition_anchor():
    item, session = shipment(), Session()
    apply(session, item, '9')
    apply(session, item, '999', NOW + timedelta(hours=1))
    assert item.tracking_status == 'requires_review'
    apply(session, item, '1', NOW + timedelta(hours=2))
    assert item.tracking_status == 'requires_review'
    assert item.last_attempt_status == 'conflicting_response'
    apply(session, item, '102', NOW + timedelta(hours=3))
    assert item.tracking_status == 'returning'


def test_undated_repeat_keeps_watermark_and_does_not_duplicate():
    item, session = shipment(), Session()
    apply(session, item, '9')
    apply(session, item, '9', None, NOW + timedelta(hours=1))
    assert item.provider_event_at == NOW
    assert len(session.events) == 1
    apply(session, item, '5', NOW - timedelta(hours=1))
    assert item.last_attempt_status == 'stale_response'


def test_different_raw_same_status_undated_keeps_watermark():
    item, session = shipment(), Session()
    apply(session, item, '9')
    apply(session, item, '10', None)
    apply(session, item, '10', None)
    assert item.provider_event_at == NOW
    assert len(session.events) == 2
    assert session.events[-1].event_at is None
    apply(session, item, '103', NOW - timedelta(hours=1))
    assert item.last_attempt_status == 'stale_response'
    assert item.tracking_status == 'delivered'



def test_total_request_deadline(monkeypatch):
    monkeypatch.setattr(tracking.settings, 'nova_poshta_timeout_seconds', 0.01)
    async def blocked(self, numbers):
        await asyncio.Event().wait()
    monkeypatch.setattr(tracking.NovaPoshtaTracking, '_fetch', blocked)
    with pytest.raises(tracking.TrackingError, match='^timeout$'):
        asyncio.run(tracking.NovaPoshtaTracking().fetch([NUMBER]))
