"""Immutable local carrier settlement and physical parcel receipt facts."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MockCODConfirmation(Base):
    __tablename__ = 'mock_cod_confirmations'
    __table_args__ = (CheckConstraint("amount_minor > 0 AND provider_event_id LIKE 'MOCK-%'", name='mock_cod_scope'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'), unique=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey('order_shipments.id', ondelete='RESTRICT'), unique=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey('order_payments.id', ondelete='RESTRICT'), unique=True)
    provider_event_id: Mapped[str] = mapped_column(String(128), unique=True)
    tracking_number: Mapped[str] = mapped_column(String(64))
    shipment_uuid: Mapped[str] = mapped_column(String(36))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    original_channel: Mapped[str] = mapped_column(String(32))
    original_simulation: Mapped[bool] = mapped_column(Boolean)
    operator_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    evidence: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockShipmentReturnReceipt(Base):
    __tablename__ = 'mock_shipment_return_receipts'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    shipment_id: Mapped[int] = mapped_column(ForeignKey('order_shipments.id', ondelete='RESTRICT'), unique=True)
    tracking_number: Mapped[str] = mapped_column(String(64))
    items: Mapped[list] = mapped_column(JSON)
    operator_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    evidence: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
