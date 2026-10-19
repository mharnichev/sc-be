"""Immutable Shop operations, distinct from local simulation and carrier mocks."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ShopPaymentConfirmation(Base):
    __tablename__ = "shop_payment_confirmations"
    __table_args__ = (UniqueConstraint("namespace", "external_reference", name="uq_shop_payment_external_reference"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("order_payments.id", ondelete="RESTRICT"), unique=True)
    namespace: Mapped[str] = mapped_column(String(128))
    external_reference: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(16))
    authority: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopFulfillmentFact(Base):
    __tablename__ = "shop_fulfillment_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey("order_handovers.id", ondelete="RESTRICT"), unique=True)
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("order_shipments.id", ondelete="RESTRICT"), unique=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    authority: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopCompletion(Base):
    __tablename__ = "shop_completions"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    payment_confirmation_id: Mapped[int] = mapped_column(ForeignKey("shop_payment_confirmations.id", ondelete="RESTRICT"))
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("shop_fulfillment_facts.id", ondelete="RESTRICT"))
    authority: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopOperationAudit(Base):
    __tablename__ = "shop_operation_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    operation: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    facts: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
