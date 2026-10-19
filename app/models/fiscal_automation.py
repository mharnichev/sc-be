"""Local ETTN simulator facts; no external provider contract is implemented."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AutomationPreview(Base):
    __tablename__ = 'mock_automation_previews'
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


class FiscalAutomation(Base):
    __tablename__ = 'mock_fiscal_automations'
    __table_args__ = (
        Index('uq_mock_automation_active_payment', 'payment_id', unique=True, postgresql_where=text("status != 'cancelled'")),
        Index('uq_mock_automation_active_order', 'order_id', unique=True, postgresql_where=text("status != 'cancelled'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    payment_id: Mapped[int] = mapped_column(ForeignKey('order_payments.id', ondelete='RESTRICT'))
    shipment_id: Mapped[int] = mapped_column(ForeignKey('order_shipments.id', ondelete='RESTRICT'))
    preview_id: Mapped[int] = mapped_column(ForeignKey('mock_automation_previews.id', ondelete='RESTRICT'), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    provider_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    cash_register_id: Mapped[str] = mapped_column(ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'))
    shift_revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default='queued')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    mock_outcome: Mapped[str] = mapped_column(String(32))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AutomationCommand(Base):
    __tablename__ = 'mock_automation_commands'
    __table_args__ = (UniqueConstraint('project_id', 'operation'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('mock_fiscal_automations.id', ondelete='RESTRICT'))
    operation: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockAutomationRecord(Base):
    """Lookup truth in the same database, deliberately not a remote adapter."""
    __tablename__ = 'mock_automation_records'
    provider_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey('mock_fiscal_automations.id', ondelete='RESTRICT'), unique=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AutomationEvent(Base):
    __tablename__ = 'mock_automation_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey('mock_fiscal_automations.id', ondelete='RESTRICT'), nullable=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    operation: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
