"""Durable local invoice simulation; no bank transport or credentials."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class QRPaymentAttempt(Base):
    __tablename__ = 'qr_payment_attempts'
    __table_args__ = (
        CheckConstraint("amount_minor > 0 AND simulation = true", name='qr_attempt_mock_positive'),
        CheckConstraint("payment_status IN ('waiting','processing','unknown','paid','declined','expired','cancelled') AND receipt_status IN ('pending','issued','error')", name='qr_attempt_states'),
        Index('uq_qr_active_order', 'order_id', unique=True, postgresql_where=text("payment_status IN ('waiting','processing','unknown','paid')")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    provider_invoice_id: Mapped[str] = mapped_column(String(36), unique=True)
    cash_register_id: Mapped[str] = mapped_column(ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'))
    shift_revision: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    payment_status: Mapped[str] = mapped_column(String(16))
    receipt_status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QRProviderRecord(Base):
    __tablename__ = 'mock_qr_provider_records'
    provider_invoice_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey('qr_payment_attempts.id', ondelete='RESTRICT'), unique=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    payment_status: Mapped[str] = mapped_column(String(16))
    receipt_status: Mapped[str] = mapped_column(String(16))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QRCommand(Base):
    __tablename__ = 'qr_payment_commands'
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey('qr_payment_attempts.id', ondelete='RESTRICT'))
    operation_key: Mapped[str] = mapped_column(String(64), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    operation: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QREvent(Base):
    __tablename__ = 'qr_payment_events'
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey('qr_payment_attempts.id', ondelete='RESTRICT'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    outcome: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
