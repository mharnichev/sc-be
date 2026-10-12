"""Privacy-preserving, server-owned Telegram booking milestones."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class TelegramBookingFunnelEvent(Base):
    __tablename__ = "telegram_booking_funnel_events"
    __table_args__ = (
        UniqueConstraint("attempt_hash", "step", name="uq_telegram_funnel_attempt_step"),
        UniqueConstraint("booking_id", name="uq_telegram_funnel_booking"),
        CheckConstraint("(step = 'booking_success' AND booking_id IS NOT NULL) OR (step <> 'booking_success' AND booking_id IS NULL)", name="telegram_funnel_success_booking"),
        Index("ix_telegram_funnel_step_occurred", "step", "occurred_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=True)
