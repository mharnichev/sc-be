"""QR contract safety and monotonic local truth, independent of providers."""
from datetime import UTC, datetime
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from app.core.config import settings
from app.schemas.qr_payment import QRCreate, QRSimulate
from app.services import qr_payment


def test_explicit_consent_and_no_client_money():
    with pytest.raises(ValidationError):
        QRCreate(expected_revision=1, cash_register_id='MOCK-KASA-1', confirm=False)
    with pytest.raises(ValidationError):
        QRCreate(expected_revision=1, cash_register_id='MOCK-KASA-1', confirm=True, amount_minor=1)
    with pytest.raises(ValidationError):
        QRCreate(expected_revision=True, cash_register_id='MOCK-KASA-1', confirm=True)
    with pytest.raises(ValidationError):
        QRSimulate(expected_revision=1, event_id='bad', outcome='success')


def test_hold_never_means_payment_and_issued_never_regresses():
    truth = SimpleNamespace(payment_status='waiting', receipt_status='pending', accepted_at=None)
    qr_payment.advance_truth(truth, 'hold')
    assert truth.payment_status == 'processing' and truth.accepted_at is None
    qr_payment.advance_truth(truth, 'success_receipt_error')
    accepted = truth.accepted_at
    assert truth.payment_status == 'paid' and truth.receipt_status == 'error'
    qr_payment.advance_truth(truth, 'expiry')
    assert truth.payment_status == 'paid' and truth.receipt_status == 'error' and truth.accepted_at == accepted
    qr_payment.advance_truth(truth, 'receipt_issued')
    qr_payment.advance_truth(truth, 'processing')
    assert truth.payment_status == 'paid' and truth.receipt_status == 'issued'


def test_mock_requires_actual_bound_local_postgres_test_database(monkeypatch):
    monkeypatch.setattr(settings, 'app_env', 'test')
    monkeypatch.setattr(settings, 'monobank_qr_mock_enabled', True)
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', True)
    monkeypatch.setattr(settings, 'local_sales_simulation_enabled', True)
    def session(url):
        return SimpleNamespace(get_bind=lambda: SimpleNamespace(url=make_url(url)))
    assert qr_payment.available(session('postgresql://u:p@127.0.0.1:55446/qr_test'))
    assert not qr_payment.available(session('postgresql://u:p@remote/qr_test'))
    assert not qr_payment.available(session('postgresql://u:p@localhost/production'))
    assert not qr_payment.available(session('sqlite:///test.db'))
    monkeypatch.setattr(settings, 'monobank_qr_mock_enabled', False)
    assert not qr_payment.available(session('postgresql://u:p@127.0.0.1/qr_test'))
