"""Durable local carrier simulation; deliberately no live Nova Poshta adapter."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, text, CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Shipment(Base):
    shop_np_return_case = relationship("ShopNPReturnCase", uselist=False, lazy="selectin")
    __tablename__ = 'order_shipments'
    __table_args__ = (Index('uq_order_active_shipment', 'order_id', unique=True, postgresql_where=text('active = true AND simulation = true'), sqlite_where=text('active = 1 AND simulation = 1')),
        Index('uq_mock_return_original_shipment', 'return_of_shipment_id', unique=True,
            postgresql_where=text('simulation = true AND return_of_shipment_id IS NOT NULL'),
            sqlite_where=text('simulation = 1 AND return_of_shipment_id IS NOT NULL')))
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    simulation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    carrier: Mapped[str] = mapped_column(String(32), default='mock_nova_poshta')
    source: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    operation_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    tracking_number: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    provider_reference: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    raw_status: Mapped[str] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    address_snapshot: Mapped[dict] = mapped_column(JSON)
    packaging: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handed_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tracking_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_status: Mapped[str] = mapped_column(String(32), default='never', server_default='never')
    tracking_failures: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_of_shipment_id: Mapped[int | None] = mapped_column(ForeignKey('order_shipments.id', ondelete='RESTRICT'), nullable=True)
    return_evidence: Mapped[str | None] = mapped_column(String(500), nullable=True)
    return_operator_id: Mapped[int | None] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'), nullable=True)
    tracking_events = relationship('TrackingEvent', lazy='selectin', order_by='TrackingEvent.id')
    shipment_items = relationship('ShipmentItem', lazy='selectin', order_by='ShipmentItem.id')
    events = relationship('ShipmentEvent', lazy='selectin', order_by='ShipmentEvent.id')
    shop_carrier_return_case = relationship('ShopCarrierReturn', foreign_keys='ShopCarrierReturn.return_shipment_id', uselist=False, lazy='selectin')

    @property
    def shop_carrier_return_case_id(self):
        from app.services.shop_carrier_returns import shipment_case_id
        return shipment_case_id(self)

    @property
    def provenance(self):
        return 'shop_carrier_return_test' if self.shop_carrier_return_case_id else None

    @property
    def authority(self):
        return 'test' if self.shop_carrier_return_case_id else None


class ShipmentEvent(Base):
    __tablename__ = 'shipment_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey('order_shipments.id', ondelete='RESTRICT'))
    operation: Mapped[str] = mapped_column(String(32))
    operation_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    raw_status: Mapped[str] = mapped_column(String(64))
    normalized_status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MockCarrierRecord(Base):
    __tablename__ = 'mock_carrier_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    tracking_number: Mapped[str] = mapped_column(String(64), unique=True)
    address_snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrackingEvent(Base):
    __tablename__ = 'shipment_tracking_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey('order_shipments.id', ondelete='RESTRICT'), index=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    raw_status: Mapped[str] = mapped_column(String(64))
    normalized_status: Mapped[str] = mapped_column(String(32))
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShipmentItem(Base):
    __tablename__ = 'shipment_items'
    __table_args__ = (UniqueConstraint('shipment_id', 'order_item_id'), CheckConstraint('quantity > 0', name='positive_quantity'))
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey('order_shipments.id', ondelete='RESTRICT'), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey('order_items.id', ondelete='RESTRICT'))
    quantity: Mapped[int] = mapped_column(Integer)


class TrackingPollState(Base):
    __tablename__ = 'shipment_tracking_poll_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    next_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
