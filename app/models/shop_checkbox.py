"""Canonical Shop Checkbox profile, separate from all legacy/local fiscal facts."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, JSON, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ShopCheckboxRegister(Base):
    __tablename__ = "shop_checkbox_registers"
    __table_args__ = (Index("uq_shop_checkbox_physical_register", text("(profile->>'register_id')"), unique=True),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    profile: Mapped[dict] = mapped_column(JSON)
    profile_hash: Mapped[str] = mapped_column(String(64))
    authority: Mapped[str] = mapped_column(String(16))
    transport: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(default=1)
    identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shift_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    shift_status: Mapped[str] = mapped_column(String(16), default="closed")


class ShopCheckboxBinding(Base):
    __tablename__ = "shop_checkbox_bindings"
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), primary_key=True)
    register_id: Mapped[str] = mapped_column(ForeignKey("shop_checkbox_registers.id", ondelete="RESTRICT"))
    profile_hash: Mapped[str] = mapped_column(String(64))
    authority: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    register = relationship("ShopCheckboxRegister", lazy="selectin")


class ShopCheckboxOperation(Base):
    __tablename__ = "shop_checkbox_operations"
    id: Mapped[int] = mapped_column(primary_key=True)
    register_id: Mapped[str] = mapped_column(ForeignKey("shop_checkbox_registers.id", ondelete="RESTRICT"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    revision: Mapped[int] = mapped_column(default=1)
    authority: Mapped[str] = mapped_column(String(16))
    transport: Mapped[str] = mapped_column(String(16))
    provider_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    shift_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acceptance = relationship("ShopCheckboxAcceptance", uselist=False, lazy="selectin")
    document = relationship("ShopCheckboxDocument", uselist=False, lazy="selectin")


class ShopCheckboxAcceptance(Base):
    __tablename__ = "shop_checkbox_acceptances"
    operation_id: Mapped[int] = mapped_column(ForeignKey("shop_checkbox_operations.id", ondelete="RESTRICT"), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), unique=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    proof: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    proof_canonical: Mapped[str] = mapped_column(Text)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopCheckboxPreview(Base):
    __tablename__ = "shop_checkbox_previews"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    return_id: Mapped[int | None] = mapped_column(ForeignKey("shop_checkbox_returns.id", ondelete="RESTRICT"), nullable=True)
    register_id: Mapped[str] = mapped_column(ForeignKey("shop_checkbox_registers.id", ondelete="RESTRICT"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    expected_revision: Mapped[int]
    register_revision: Mapped[int]
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopCheckboxDocument(Base):
    __tablename__ = "shop_checkbox_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    payment_confirmation_id: Mapped[int] = mapped_column(ForeignKey("shop_payment_confirmations.id", ondelete="RESTRICT"))
    return_id: Mapped[int | None] = mapped_column(ForeignKey("shop_checkbox_returns.id", ondelete="RESTRICT"), nullable=True)
    preview_id: Mapped[int] = mapped_column(ForeignKey("shop_checkbox_previews.id", ondelete="RESTRICT"), unique=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("shop_checkbox_operations.id", ondelete="RESTRICT"), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    operation = relationship("ShopCheckboxOperation", lazy="selectin", overlaps="document")


class ShopCheckboxReturn(Base):
    __tablename__ = "shop_checkbox_returns"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    sale_document_id: Mapped[int] = mapped_column(ForeignKey("shop_checkbox_documents.id", ondelete="RESTRICT"))
    register_id: Mapped[str] = mapped_column(ForeignKey("shop_checkbox_registers.id", ondelete="RESTRICT"))
    shift_id: Mapped[str] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(default=1)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    lines: Mapped[list] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancellation_case_id: Mapped[int | None] = mapped_column(ForeignKey("shop_payment_resolution_cases.id", ondelete="RESTRICT"), unique=True, nullable=True)
    carrier_receipt_id: Mapped[int | None] = mapped_column(ForeignKey("shop_np_return_receipts.id", ondelete="RESTRICT"),unique=True,nullable=True)
    refund_fact = relationship("ShopCheckboxRefundFact",uselist=False,lazy="selectin")
    documents = relationship("ShopCheckboxDocument",foreign_keys="ShopCheckboxDocument.return_id",lazy="selectin",order_by="ShopCheckboxDocument.id")


class ShopCheckboxRefundFact(Base):
    __tablename__ = "shop_checkbox_refund_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("shop_checkbox_returns.id", ondelete="RESTRICT"), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(32))
    authority: Mapped[str] = mapped_column(String(16))
    external_reference: Mapped[str] = mapped_column(String(200), unique=True)
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopCheckboxPhysicalReceipt(Base):
    __tablename__ = "shop_checkbox_physical_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("shop_checkbox_returns.id", ondelete="RESTRICT"), unique=True)
    lines: Mapped[list] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopCheckboxAudit(Base):
    __tablename__ = "shop_checkbox_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    operation: Mapped[str] = mapped_column(String(40))
    facts: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
