"""Isolated mock fiscal records. No production/provider credentials exist here."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MockCashRegister(Base):
    __tablename__ = 'mock_cash_registers'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    credential_kind: Mapped[str] = mapped_column(String(16), default='mock')
    shift_status: Mapped[str] = mapped_column(String(16), default='closed')
    shift_revision: Mapped[int] = mapped_column(Integer, default=0)


class FiscalPreview(Base):
    __tablename__ = 'fiscal_previews'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    revision: Mapped[int] = mapped_column(Integer)
    cash_register_id: Mapped[str] = mapped_column(ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'))
    shift_revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FiscalDocument(Base):
    __tablename__ = 'fiscal_documents'
    __table_args__ = (UniqueConstraint('preview_id'),
        Index('uq_fiscal_active_order', 'order_id', unique=True, postgresql_where=text("status != 'cancelled' AND kind = 'sale'")),
        Index('uq_fiscal_active_return', 'return_id', unique=True, postgresql_where=text("status != 'cancelled' AND kind = 'return'")),
        Index('uq_fiscal_active_payment', 'payment_id', unique=True, postgresql_where=text("status != 'cancelled' AND kind = 'sale'")))
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    qr_attempt_id: Mapped[int | None] = mapped_column(ForeignKey('qr_payment_attempts.id', ondelete='RESTRICT'), nullable=True, unique=True)
    automation_id: Mapped[int | None] = mapped_column(ForeignKey('mock_fiscal_automations.id', ondelete='RESTRICT'), nullable=True, unique=True)
    kind: Mapped[str] = mapped_column(String(16), default='sale', server_default='sale')
    parent_sale_id: Mapped[int | None] = mapped_column(ForeignKey('fiscal_documents.id', ondelete='RESTRICT'), nullable=True)
    return_id: Mapped[int | None] = mapped_column(ForeignKey('mock_returns.id', ondelete='RESTRICT', use_alter=True, name='fk_fiscal_document_return'), nullable=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey('order_payments.id', ondelete='RESTRICT'))
    preview_id: Mapped[int] = mapped_column(ForeignKey('fiscal_previews.id', ondelete='RESTRICT'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    provider_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    cash_register_id: Mapped[str] = mapped_column(ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'))
    shift_revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='queued')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    mock_outcome: Mapped[str] = mapped_column(String(32))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(32), default='pending_document')
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_error: Mapped[str | None] = mapped_column(String(64), nullable=True)


class FiscalJob(Base):
    __tablename__ = 'fiscal_jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('fiscal_documents.id', ondelete='RESTRICT'), unique=True)
    status: Mapped[str] = mapped_column(String(16), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockReceipt(Base):
    __tablename__ = 'mock_receipts'
    provider_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('fiscal_documents.id', ondelete='RESTRICT'), unique=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FiscalEvent(Base):
    __tablename__ = 'fiscal_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey('fiscal_documents.id', ondelete='RESTRICT'), nullable=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    operation: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FiscalInbox(Base):
    __tablename__ = 'fiscal_inbox'
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_uuid: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReturnRecord(Base):
    """Immutable quantity reservation; cancellation of fiscal approval releases nothing."""
    __tablename__ = 'mock_returns'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    parent_sale_id: Mapped[int] = mapped_column(ForeignKey('fiscal_documents.id', ondelete='RESTRICT'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    items: Mapped[list] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockRefund(Base):
    __tablename__ = 'mock_refunds'
    id: Mapped[int] = mapped_column(primary_key=True)
    qr_attempt_id: Mapped[int | None] = mapped_column(ForeignKey('qr_payment_attempts.id', ondelete='RESTRICT'), nullable=True)
    return_id: Mapped[int] = mapped_column(ForeignKey('mock_returns.id', ondelete='RESTRICT'), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockReturnReceipt(Base):
    __tablename__ = 'mock_return_receipts'
    shipment_return_receipt_id: Mapped[int | None] = mapped_column(ForeignKey('mock_shipment_return_receipts.id', ondelete='RESTRICT'), nullable=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey('mock_returns.id', ondelete='RESTRICT'), unique=True)
    items: Mapped[list] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockFiscalReport(Base):
    __tablename__ = 'mock_fiscal_reports'
    __table_args__ = (Index('uq_mock_z_report', 'cash_register_id', 'shift_revision', unique=True, postgresql_where=text("kind = 'z'")),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(1))
    cash_register_id: Mapped[str] = mapped_column(ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'))
    shift_revision: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    totals: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReconciliationAcknowledgement(Base):
    __tablename__ = 'mock_reconciliation_acknowledgements'
    id: Mapped[int] = mapped_column(primary_key=True)
    case_type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(500))
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockFiscalShare(Base):
    __tablename__ = 'mock_fiscal_shares'
    __table_args__ = (UniqueConstraint('document_id', 'generation', name='uq_mock_fiscal_share_generation'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('fiscal_documents.id', ondelete='RESTRICT'))
    generation: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scope: Mapped[str] = mapped_column(String(32), default='mock_fiscal_document')
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockFiscalShareRevocation(Base):
    __tablename__ = 'mock_fiscal_share_revocations'
    id: Mapped[int] = mapped_column(primary_key=True)
    share_id: Mapped[int] = mapped_column(ForeignKey('mock_fiscal_shares.id', ondelete='RESTRICT'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
