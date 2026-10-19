"""Existing reverse parcels, carrier observations and separate physical inspection."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class ShopNPReturnCase(Base):
    __tablename__ = "shop_np_return_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    original_shipment_id: Mapped[int] = mapped_column(ForeignKey("order_shipments.id", ondelete="RESTRICT"), unique=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("shop_fulfillment_facts.id", ondelete="RESTRICT"))
    sale_document_id: Mapped[int | None] = mapped_column(ForeignKey("shop_checkbox_documents.id", ondelete="RESTRICT"), nullable=True)
    register_id: Mapped[str] = mapped_column(ForeignKey("shop_checkbox_registers.id", ondelete="RESTRICT"))
    register_profile_hash: Mapped[str] = mapped_column(String(64))
    original_ttn: Mapped[str] = mapped_column(String(14))
    return_ttn: Mapped[str] = mapped_column(String(14), unique=True)
    financial_mode: Mapped[str] = mapped_column(String(16))
    linkage_kind: Mapped[str] = mapped_column(String(24))
    manual_link: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    profile_id: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSON)
    profile_hash: Mapped[str] = mapped_column(String(64))
    authority: Mapped[str] = mapped_column(String(16))
    transport: Mapped[str] = mapped_column(String(16))
    test_context: Mapped[bool]
    revision: Mapped[int] = mapped_column(default=1)
    lines: Mapped[list] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observations = relationship("ShopNPReturnObservation", lazy="selectin", order_by="ShopNPReturnObservation.id")
    receipt = relationship("ShopNPReturnReceipt", uselist=False, lazy="selectin")
    closure = relationship("ShopNPUnpaidClosure", uselist=False, lazy="selectin")


class ShopNPReturnObservation(Base):
    __tablename__ = "shop_np_return_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("shop_np_return_cases.id", ondelete="RESTRICT"))
    proof: Mapped[dict] = mapped_column(JSON)
    proof_canonical: Mapped[str] = mapped_column(Text)
    proof_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopNPReturnReceipt(Base):
    __tablename__ = "shop_np_return_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("shop_np_return_cases.id", ondelete="RESTRICT"), unique=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("shop_np_return_observations.id", ondelete="RESTRICT"))
    lines: Mapped[list] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopNPUnpaidClosure(Base):
    __tablename__ = "shop_np_unpaid_closures"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("shop_np_return_cases.id", ondelete="RESTRICT"), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("shop_np_return_receipts.id", ondelete="RESTRICT"), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopNPReturnAudit(Base):
    __tablename__ = "shop_np_return_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("shop_np_return_cases.id", ondelete="RESTRICT"))
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    operation: Mapped[str] = mapped_column(String(32))
    facts: Mapped[dict] = mapped_column(JSON)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
