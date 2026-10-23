from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ShopIngressAdmission(Base):
    """Only admitted events; source identity is a server-keyed, action-scoped digest."""

    __tablename__ = "shop_ingress_admissions"
    __table_args__ = (
        CheckConstraint("action IN ('otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart', 'reminder', 'reminder_email')", name="action"),
        CheckConstraint("source_key ~ '^[0-9a-f]{64}$'", name="source_key"),
        Index("ix_shop_ingress_source_window", "source_key", "action", "admitted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp(),
    )
