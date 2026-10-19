"""Remote test-provider evidence; local fiscal rows are projections of this run."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class SandboxRun(Base):
    __tablename__ = 'provider_sandbox_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey('qr_payment_attempts.id', ondelete='RESTRICT'), unique=True)
    receipt_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    invoice_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    payment_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    invoice_dispatched: Mapped[bool] = mapped_column(Boolean, default=False)
    receipt_dispatched: Mapped[bool] = mapped_column(Boolean, default=False)
    verification: Mapped[dict] = mapped_column(JSON)
    payment_status: Mapped[str] = mapped_column(String(16), default='waiting')
    receipt_status: Mapped[str] = mapped_column(String(16), default='pending')
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_evidence: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    receipt_evidence: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
