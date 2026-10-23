"""Opt-in, manual-only technical incidents. Never serialize business payloads."""
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from time import time

from starlette.exceptions import HTTPException

poll_context = ContextVar("technical_poll", default=False)
_enabled = False
_store = None


def before_send(event, hint):
    # Rebuild from an allowlist: SDK processors/scopes cannot append PII.
    if event.get('logger') != 'backend.technical':
        return None
    tags = event.get('tags', {})
    step, code = tags.get('step'), tags.get('incident_code')
    if step not in STEPS or code not in CODES:
        return None
    operation = event.get('contexts', {}).get('operation', {})
    metadata = {}
    event_id = event.get('event_id')
    if isinstance(event_id, str) and re.fullmatch(r'[0-9a-f]{32}', event_id):
        metadata['event_id'] = event_id
    if event.get('environment') in {'local', 'test', 'staging', 'production'}:
        metadata['environment'] = event['environment']
    return {**metadata, 'logger': 'backend.technical', 'level': 'error', 'platform': 'python',
        'message': f'Backend technical incident: {step}/{code}',
        'fingerprint': ['backend', step, code],
        'tags': {'flow': 'backend' if step == 'request' else 'shop', 'step': step, 'incident_code': code,
            **({'payment_method': 'onlineCard'} if step == 'invoice_create' else {}),
            'provider': 'monobank' if step in {'invoice_create', 'payment_verify', 'receipt_verify', 'paid_save', 'poll'} else 'backend'},
        'contexts': {'operation': {key: operation[key] for key in ('order_id', 'attempt_id')
            if type(operation.get(key)) is int and operation[key] > 0}}}


def initialize(settings):
    global _enabled, _store
    # SDK/client initialization belongs exclusively to app.core.sentry.
    _enabled = bool((settings.sentry_dsn or '').strip())
    _store = settings.sentry_incident_store


STEPS = {'order_create', 'invoice_create', 'payment_verify', 'paid_save',
    'receipt_verify', 'reconciliation', 'webhook', 'poll', 'request'}
CODES = {'unexpected', 'provider_unavailable', 'invoice_unknown', 'proof_invalid',
    'paid_unconfirmed', 'poll_failed', 'signature_invalid'}


def report(step, code='unexpected', *, order_id=None, attempt_id=None, repeated=False):
    """Shared durable store: <=20/hour and 30/day, one incident/step/code/order/15min, >=3 poll failures.

    IDs are server-owned integer ledger IDs only, never invoice IDs or tokens.
    Reporting must not affect a financial operation, including if SDK fails.
    """
    if not _enabled or step not in STEPS or code not in CODES:
        return
    order_id = order_id if type(order_id) is int and order_id > 0 else None
    attempt_id = attempt_id if type(attempt_id) is int and attempt_id > 0 else None
    key = sha256(repr((step, code, order_id, attempt_id)).encode()).hexdigest()
    if not admit(key, repeated=repeated or poll_context.get() and step != "paid_save"):
        return
    try:
        import sentry_sdk
        sentry_sdk.capture_event({'logger': 'backend.technical', 'level': 'error',
            'message': f'Backend technical incident: {step}/{code}',
            'fingerprint': ['backend', step, code],
            'tags': {'flow': 'shop', 'step': step, 'incident_code': code, 'provider': 'monobank' if step != 'order_create' else 'backend'},
            'contexts': {'operation': {k: v for k, v in {'order_id': order_id,
                'attempt_id': attempt_id}.items() if v is not None}}})
    except Exception:
        pass


def report_exception(step, error, *, order_id=None, attempt_id=None, repeated=False):
    if step not in STEPS or step == 'request':
        # Nonfinancial errors retain the existing framework/logger diagnostics.
        return
    if getattr(error, "_technical_reported", False):
        return
    error._technical_reported = True
    # Expected access/validation/conflicts, including bad caller input, stay local.
    if isinstance(error, HTTPException):
        detail = error.detail if isinstance(error.detail, dict) else {}
        if detail.get('code') == 'monobank_proof_invalid':
            report(step, 'proof_invalid', order_id=order_id, attempt_id=attempt_id, repeated=True)
        elif error.status_code >= 500:
            report(step, order_id=order_id, attempt_id=attempt_id, repeated=repeated)
        return
    report(step, order_id=order_id, attempt_id=attempt_id, repeated=repeated)


def provider_failure(step, code, *, order_id=None, attempt_id=None):
    mapped = {'monobank_unavailable': 'provider_unavailable',
        'monobank_invoice_create_unknown': 'invoice_unknown',
        'monobank_proof_invalid': 'proof_invalid', 'monobank_profile_mismatch': 'proof_invalid'}
    if code in mapped:
        report(step, mapped[code], order_id=order_id, attempt_id=attempt_id, repeated=step != 'invoice_create')


def admit(key, *, repeated=False):
    """Reserve before sending. Shared SQLite path must be on persistent local disk.

    Fail closed on I/O/locking errors. No raw exception, request, or provider data
    enters this store. Concurrent workers and restarts retain the same budget.
    """
    try:
        path = Path(_store)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(path, timeout=0.1) as db:
            db.execute("CREATE TABLE IF NOT EXISTS incidents (key TEXT PRIMARY KEY, count INTEGER, sent REAL, last REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS budget (sent REAL)")
            db.execute("BEGIN IMMEDIATE")
            now = time()
            db.execute("DELETE FROM incidents WHERE last < ?", (now - 900,))
            db.execute("DELETE FROM budget WHERE sent < ?", (now - 86400,))
            row = db.execute("SELECT count,sent FROM incidents WHERE key=?", (key,)).fetchone()
            if row is None and db.execute("SELECT count(*) FROM incidents").fetchone()[0] >= 2048:
                return False
            count, sent = (row[0] + 1, row[1]) if row else (1, None)
            db.execute("INSERT OR REPLACE INTO incidents VALUES(?,?,?,?)", (key, count, sent, now))
            allowed = not (repeated and count < 3 or sent is not None and now - sent < 900)
            if (allowed and db.execute("SELECT count(*) FROM budget").fetchone()[0] < 30
                    and db.execute("SELECT count(*) FROM budget WHERE sent >= ?", (now - 3600,)).fetchone()[0] < 20):
                db.execute("UPDATE incidents SET sent=? WHERE key=?", (now, key))
                db.execute("INSERT INTO budget VALUES(?)", (now,))
            else:
                allowed = False
            # Reject excess keys instead of evicting active dedup reservations.
            return allowed
    except Exception:
        return False
