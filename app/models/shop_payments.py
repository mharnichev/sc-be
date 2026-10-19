"""Pinned payment intent and immutable R2 verification evidence."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class ShopPaymentIntent(Base):
    __tablename__ = "shop_payment_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    mode: Mapped[str] = mapped_column(String(16))
    reference: Mapped[str] = mapped_column(String(36), unique=True)
    amount_minor: Mapped[int]
    profile_id: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSON)
    profile_hash: Mapped[str] = mapped_column(String(64))
    authority: Mapped[str] = mapped_column(String(16))
    transport: Mapped[str] = mapped_column(String(16))
    invoice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("profile_id", "invoice_id", name="uq_shop_invoice_binding"),)


class ShopPaymentEvidence(Base):
    __tablename__ = "shop_payment_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("shop_payment_intents.id", ondelete="RESTRICT"), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("order_payments.id", ondelete="RESTRICT"), unique=True)
    correction_id: Mapped[int | None] = mapped_column(ForeignKey("shop_payment_invoice_corrections.id", ondelete="RESTRICT"), nullable=True, unique=True)
    correction = relationship("ShopPaymentInvoiceCorrection", lazy="selectin")
    identity: Mapped[str] = mapped_column(String(64), unique=True)
    authority: Mapped[str] = mapped_column(String(16))
    proof: Mapped[dict] = mapped_column(JSON)
    proof_canonical: Mapped[str] = mapped_column(Text)
    proof_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
