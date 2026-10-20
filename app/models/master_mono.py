"""Separate POS ledger for externally issued mono receipts."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class MasterMonoBinding(Base):
    __tablename__ = "master_mono_bindings"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    invoice_id: Mapped[str] = mapped_column(String(100), unique=True)
    reference: Mapped[str] = mapped_column(String(36), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    profile_id: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSON)
    authority: Mapped[str] = mapped_column(String(16), default="test")
    transport: Mapped[str] = mapped_column(String(16), default="mock_http")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    create_request_id: Mapped[int | None] = mapped_column(ForeignKey("master_mono_create_requests.id", ondelete="RESTRICT"), unique=True, nullable=True)
    page_url: Mapped[str | None] = mapped_column(String(240), nullable=True)
    create_proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MasterMonoPayment(Base):
    __tablename__ = "master_mono_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("master_mono_bindings.id", ondelete="RESTRICT"), unique=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("order_payments.id", ondelete="RESTRICT"), unique=True)
    proof: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MasterMonoReceipt(Base):
    __tablename__ = "master_mono_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("master_mono_bindings.id", ondelete="RESTRICT"), unique=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("order_payments.id", ondelete="RESTRICT"), unique=True)
    receipt_id: Mapped[str] = mapped_column(String(36), unique=True)
    proof: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    artifact = relationship("MasterMonoArtifact", uselist=False, lazy="selectin")


class MasterMonoArtifact(Base):
    __tablename__ = "master_mono_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    receipt_id: Mapped[int] = mapped_column(ForeignKey("master_mono_receipts.id", ondelete="RESTRICT"), unique=True)
    proof: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MasterMonoObservation(Base):
    __tablename__ = "master_mono_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(64))
    proof: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MasterMonoCreateRequest(Base):
    """One durable dispatch claim per order; even an unknown outcome owns it."""
    __tablename__ = "master_mono_create_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    reference: Mapped[str] = mapped_column(String(36), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    profile_id: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSON)
    authority: Mapped[str] = mapped_column(String(16))
    transport: Mapped[str] = mapped_column(String(16))
    basket: Mapped[list] = mapped_column(JSON)
    basket_hash: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
