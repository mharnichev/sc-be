"""Offline telemetry policy; fake SDK, synthetic identifiers, no database/provider."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.exceptions import HTTPException

spec = importlib.util.spec_from_file_location('isolated_reporting', Path(__file__).parents[1] / 'app/core/error_reporting.py')
reporting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporting)


@pytest.fixture
def captured(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(reporting, '_enabled', True)
    monkeypatch.setattr(reporting, '_store', str(tmp_path / 'private/incidents.sqlite3'))
    monkeypatch.setitem(sys.modules, 'sentry_sdk', SimpleNamespace(capture_event=events.append))
    return events


def test_expected_errors_and_provider_declines_are_silent(captured):
    for status in [400, 401, 403, 404, 409, 422, 429]:
        reporting.report_exception('request', HTTPException(status, 'secret customer data'))
    for code in ['failure', 'expired', 'reversed', 'monobank_not_found', 'monobank_unconfigured']:
        reporting.provider_failure('payment_verify', code, order_id=1)
    assert captured == []


def test_poll_threshold_dedup_and_restart(captured):
    for _ in range(2):
        reporting.provider_failure('payment_verify', 'monobank_unavailable', order_id=1)
    assert captured == []
    reporting.provider_failure('payment_verify', 'monobank_unavailable', order_id=1)
    assert len(captured) == 1
    # No in-memory counter: reopening same store preserves suppression.
    for _ in range(10):
        reporting.provider_failure('payment_verify', 'monobank_unavailable', order_id=1)
    assert len(captured) == 1


def test_global_durable_budget_and_concurrent_admission(captured):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: reporting.report('invoice_create', order_id=n+1), range(40)))
    assert 0 < len(captured) <= 20
    reporting.report('request', order_id=1000)
    assert len(captured) <= 20


def test_nested_errors_capture_once_and_never_serialize_exception(captured):
    error = RuntimeError('token=secret, https://pay.example/sensitive')
    reporting.report_exception('payment_verify', error, order_id=12)
    reporting.report_exception('request', error)
    assert len(captured) == 1
    assert 'secret' not in repr(captured)
    assert captured[0]['contexts']['operation'] == {'order_id': 12}


def test_nonfinancial_errors_keep_framework_ownership_with_unavailable_store(captured, monkeypatch, tmp_path):
    monkeypatch.setattr(reporting, '_store', str(tmp_path))
    error = RuntimeError('ordinary diagnostic')
    reporting.report_exception('request', error)
    assert not getattr(error, '_technical_reported', False)
    assert captured == []


def test_cardinality_pressure_cannot_evict_active_dedup(captured, monkeypatch):
    now = [100000.0]
    monkeypatch.setattr(reporting, 'time', lambda: now[0])
    reporting.report('invoice_create', order_id=1)
    now[0] += 1
    for order_id in range(2, 2055):
        reporting.report('payment_verify', order_id=order_id, repeated=True)
    reporting.report('invoice_create', order_id=1)
    assert len(captured) == 1
    import sqlite3
    with sqlite3.connect(reporting._store) as db:
        assert db.execute('SELECT count(*) FROM incidents').fetchone()[0] == 2048


def test_allowlist_removes_scope_and_sdk_data(captured):
    reporting.report('invoice_create', order_id=12, attempt_id='token')
    event = captured[0]
    event.update(request={'data': 'secret'}, user={'email': 'secret'}, extra={'token': 'secret'},
        exception={'values': [{'value': 'secret'}]}, breadcrumbs={'values': ['secret']})
    event['contexts']['unsafe'] = {'token': 'secret'}
    event['contexts']['operation']['email'] = 'secret'
    event['tags']['token'] = 'secret'
    safe = reporting.before_send(event, {})
    assert 'secret' not in repr(safe)
    assert safe['contexts']['operation'] == {'order_id': 12}
    assert reporting.before_send({'logger': 'uvicorn.error'}, {}) is None


def test_store_or_sdk_failure_cannot_break_payment(captured, monkeypatch, tmp_path):
    monkeypatch.setattr(reporting, '_store', str(tmp_path))
    reporting.report('invoice_create')
    assert captured == []
    monkeypatch.setattr(reporting, '_store', str(tmp_path / 'state.sqlite3'))
    monkeypatch.setitem(sys.modules, 'sentry_sdk', SimpleNamespace(capture_event=lambda event: 1 / 0))
    reporting.report('invoice_create')


def test_initialize_only_configures_manual_reporting_without_second_client(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, 'sentry_sdk', SimpleNamespace(init=lambda **kw: calls.append(kw)))
    reporting.initialize(SimpleNamespace(sentry_dsn=None, sentry_incident_store='/tmp/fixture.sqlite3'))
    assert not reporting._enabled
    reporting.initialize(SimpleNamespace(sentry_dsn='fixture', sentry_incident_store='/tmp/fixture.sqlite3'))
    assert reporting._enabled
    assert not calls


def test_poll_service_exception_counts_only_once_per_attempt(captured):
    for _ in range(3):
        token = reporting.poll_context.set(True)
        try:
            error = RuntimeError('secret')
            reporting.report_exception('payment_verify', error, order_id=3)
            assert error._technical_reported
            # Outer HTTP capture also sees the same error, but must not duplicate.
            reporting.report_exception('request', error)
        finally:
            reporting.poll_context.reset(token)
    assert len(captured) == 1
    assert captured[0]['tags']['step'] == 'payment_verify'


def test_paid_persistence_failure_is_immediate_even_in_poll(captured):
    token = reporting.poll_context.set(True)
    try:
        reporting.report('paid_save', 'paid_unconfirmed', order_id=7)
    finally:
        reporting.poll_context.reset(token)
    assert len(captured) == 1


def test_real_sdk_transport_sends_only_sanitized_manual_event(tmp_path, monkeypatch):
    import sentry_sdk
    from sentry_sdk.transport import Transport
    events = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type == 'event':
                    events.append(item.get_event())

    real_init = sentry_sdk.init
    guards = []
    def init(**options):
        guard = real_init(transport=MemoryTransport, **options)
        guards.append(guard)
        return guard
    monkeypatch.setattr(sentry_sdk, 'init', init)
    monkeypatch.setattr(reporting, '_enabled', False)
    monkeypatch.setattr(reporting, '_store', None)
    try:
        from app.core import sentry as sentry_module
        monkeypatch.setattr(sentry_module, 'error_reporting', reporting)
        monkeypatch.setattr(sentry_module.settings, 'sentry_dsn', 'https://fixture@example.invalid/1')
        monkeypatch.setattr(sentry_module.settings, 'sentry_incident_store', str(tmp_path / 'sdk.sqlite3'))
        monkeypatch.setattr(sentry_module.settings, 'app_env', 'test')
        sentry_module.init_sentry('api')
        with sentry_sdk.new_scope() as scope:
            scope.set_user({'email': 'secret@example.invalid'})
            scope.set_context('unsafe', {'token': 'secret'})
            scope.set_tag('unsafe', 'secret')
            propagated = RuntimeError('secret')
            propagated._technical_reported = True
            sentry_sdk.capture_exception(propagated)
            import logging
            try:
                raise propagated
            except RuntimeError:
                logging.getLogger('app.services.shop_payments').exception('synthetic duplicate financial error')
            reporting.report('paid_save', 'paid_unconfirmed', order_id=19)
        assert len(events) == 1
        assert events[0]['event_id']
        assert events[0]['environment'] == 'test'
        assert events[0]['contexts']['operation'] == {'order_id': 19}
        assert 'secret' not in repr(events)
        monkeypatch.setattr(reporting, '_store', str(tmp_path))
        ordinary = RuntimeError('ordinary diagnostic')
        reporting.report_exception('request', ordinary)
        sentry_sdk.capture_exception(ordinary)
        assert len(events) == 2
        assert events[1]['exception']['values'][0]['type'] == 'RuntimeError'
    finally:
        for guard in guards:
            guard.__exit__(None, None, None)


def test_daily_budget_survives_hour_window_and_expires(captured, monkeypatch):
    now = [100000.0]
    monkeypatch.setattr(reporting, 'time', lambda: now[0])
    for n in range(25):
        reporting.report('invoice_create', order_id=n+1)
    assert len(captured) == 20
    now[0] += 3601
    for n in range(25):
        reporting.report('invoice_create', order_id=n+100)
    assert len(captured) == 30
    now[0] += 86401
    reporting.report('invoice_create', order_id=999)
    assert len(captured) == 31
