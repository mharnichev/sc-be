"""Canonical Shop return bindings with explicit local-test authority."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ShopCarrierReturn(Base):
    __tablename__ = "shop_carrier_returns"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    original_shipment_id: Mapped[int] = mapped_column(ForeignKey("order_shipments.id", ondelete="RESTRICT"), unique=True)
    return_shipment_id: Mapped[int] = mapped_column(ForeignKey("order_shipments.id", ondelete="RESTRICT"), unique=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("shop_fulfillment_facts.id", ondelete="RESTRICT"))
    sale_document_id: Mapped[int | None] = mapped_column(ForeignKey("shop_fiscal_documents.id", ondelete="RESTRICT"), nullable=True)
    shift_revision: Mapped[int | None] = mapped_column(nullable=True)
    financial_mode: Mapped[str] = mapped_column(String(16))
    authority: Mapped[str] = mapped_column(String(16))
    provider_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20))
    lines: Mapped[list] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fulfillment = relationship("ShopFulfillmentFact", lazy="selectin")
    acceptance = relationship("ShopCarrierReturnAcceptance", uselist=False, lazy="selectin")
    receipt = relationship("ShopCarrierReturnReceipt", uselist=False, lazy="selectin")
    closure = relationship("ShopCarrierUnpaidClosure", uselist=False, lazy="selectin")


class ShopCarrierReturnAcceptance(Base):
    __tablename__ = "shop_carrier_return_acceptances"
    provider_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("shop_carrier_returns.id", ondelete="RESTRICT"), unique=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    tracking_number: Mapped[str] = mapped_column(String(64), unique=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopCarrierReturnReceipt(Base):
    __tablename__ = "shop_carrier_return_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("shop_carrier_returns.id", ondelete="RESTRICT"), unique=True)
    lines: Mapped[list] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopCarrierUnpaidClosure(Base):
    __tablename__ = "shop_carrier_unpaid_closures"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("shop_carrier_returns.id", ondelete="RESTRICT"), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("shop_carrier_return_receipts.id", ondelete="RESTRICT"), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
