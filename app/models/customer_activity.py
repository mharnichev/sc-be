from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class CustomerActivityAccessToken(TimestampMixin, Base):
    """Hash-only capability used by the unauthenticated booking management UI."""

    __tablename__ = "customer_activity_access_tokens"
    __table_args__ = (
        Index("ix_customer_activity_access_tokens_customer_active", "customer_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_waitlist_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("waitlist_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_recipients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    customer = relationship("Customer")
    source_booking = relationship("Booking")
    source_waitlist_request = relationship("WaitlistRequest")
    recipient = relationship("MessageRecipient")
