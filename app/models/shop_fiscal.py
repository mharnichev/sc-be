"""Shop-only local fiscal facts. No legacy mock receipt can satisfy these links."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ShopFiscalRegister(Base):
    __tablename__ = "shop_fiscal_registers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shift_status: Mapped[str] = mapped_column(String(16), default="closed")
    shift_revision: Mapped[int] = mapped_column(default=1)


class ShopFiscalPreview(Base):
    __tablename__ = "shop_fiscal_previews"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    return_id: Mapped[int | None] = mapped_column(ForeignKey("shop_financial_returns.id", ondelete="RESTRICT"), nullable=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    expected_revision: Mapped[int]
    shift_revision: Mapped[int]
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopFiscalDocument(Base):
    __tablename__ = "shop_fiscal_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    payment_confirmation_id: Mapped[int] = mapped_column(ForeignKey("shop_payment_confirmations.id", ondelete="RESTRICT"))
    return_id: Mapped[int | None] = mapped_column(ForeignKey("shop_financial_returns.id", ondelete="RESTRICT"), nullable=True)
    preview_id: Mapped[int] = mapped_column(ForeignKey("shop_fiscal_previews.id", ondelete="RESTRICT"), unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    revision: Mapped[int] = mapped_column(default=1)
    provider_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    shift_revision: Mapped[int]
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acceptance = relationship("ShopFiscalAcceptance", uselist=False, lazy="selectin")


class ShopFiscalAcceptance(Base):
    __tablename__ = "shop_fiscal_acceptances"
    provider_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("shop_fiscal_documents.id", ondelete="RESTRICT"), unique=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopFinancialReturn(Base):
    __tablename__ = "shop_financial_returns"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    sale_document_id: Mapped[int] = mapped_column(ForeignKey("shop_fiscal_documents.id", ondelete="RESTRICT"))
    carrier_receipt_id: Mapped[int | None] = mapped_column(ForeignKey("shop_carrier_return_receipts.id", ondelete="RESTRICT"), nullable=True)
    revision: Mapped[int] = mapped_column(default=1)
    shift_revision: Mapped[int]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    lines: Mapped[list] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopRefundFact(Base):
    __tablename__ = "shop_refund_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("shop_financial_returns.id", ondelete="RESTRICT"), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(32))
    external_reference: Mapped[str] = mapped_column(String(200), unique=True)
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopReturnReceipt(Base):
    __tablename__ = "shop_return_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("shop_financial_returns.id", ondelete="RESTRICT"), unique=True)
    carrier_receipt_id: Mapped[int | None] = mapped_column(ForeignKey("shop_carrier_return_receipts.id", ondelete="RESTRICT"), nullable=True)
    lines: Mapped[list] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopFiscalReport(Base):
    __tablename__ = "shop_fiscal_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(1))
    shift_revision: Mapped[int]
    totals: Mapped[dict] = mapped_column(JSON)
    reconciliation: Mapped[dict] = mapped_column(JSON)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopFiscalAudit(Base):
    __tablename__ = "shop_fiscal_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    operation: Mapped[str] = mapped_column(String(40))
    facts: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
