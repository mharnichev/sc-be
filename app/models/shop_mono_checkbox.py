"""Immutable externally issued receipt and append-only provider observations."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class ShopMonoCheckboxReceipt(Base):
    __tablename__ = "shop_mono_checkbox_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    payment_confirmation_id: Mapped[int] = mapped_column(ForeignKey("shop_payment_confirmations.id", ondelete="RESTRICT"), unique=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("shop_payment_evidence.id", ondelete="RESTRICT"), unique=True)
    profile_id: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    invoice_id: Mapped[str] = mapped_column(String(128))
    receipt_id: Mapped[str] = mapped_column(String(36))
    authority: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    proof: Mapped[dict] = mapped_column(JSON)
    proof_canonical: Mapped[str] = mapped_column(Text)
    proof_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    artifact = relationship("ShopMonoCheckboxArtifact", uselist=False, lazy="selectin")
    __table_args__ = (UniqueConstraint("receipt_id", name="uq_shop_mono_checkbox_receipt_identity"),)


class ShopMonoCheckboxObservation(Base):
    __tablename__ = "shop_mono_checkbox_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(64))
    proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proof_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopMonoCheckboxArtifact(Base):
    """First later-arriving PDF; accepted receipt metadata is never overwritten."""
    __tablename__ = "shop_mono_checkbox_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("shop_mono_checkbox_receipts.id", ondelete="RESTRICT"), unique=True)
    proof: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
