"""Automatic one-checkout service reminder; not a marketing audience or account session."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin


class CheckoutReminder(TimestampMixin, Base):
    __tablename__ = "checkout_reminders"
    __table_args__ = (
        CheckConstraint("state IN ('pending','attempting','retry','sent','dry_run','uncertain','cancelled','completed','expired','suppressed')", name="checkout_reminder_state"),
        CheckConstraint("attempts >= 0 AND attempts <= 3", name="checkout_reminder_attempts"),
        CheckConstraint("checkpoint_revision >= 0 AND checkpoint_revision <= 2147483647", name="checkout_reminder_revision"),
        Index("ix_checkout_reminders_due", "state", "due_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    management_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    locale: Mapped[str] = mapped_column(String(2), nullable=False, default="uk")
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checkpoint_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    snapshot: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    recovery_management_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
