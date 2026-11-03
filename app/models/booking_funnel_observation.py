"""Client-reported diagnostics; deliberately separate from historical funnel milestones."""
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BookingFunnelObservation(Base):
    __tablename__ = "booking_funnel_observations"
    __table_args__ = (
        UniqueConstraint("event_id_hash"),
        UniqueConstraint("dedup_key"),
        CheckConstraint("event_type IN ('time_step_viewed','availability_requested','availability_result','availability_failed','availability_cancelled')", name="funnel_observation_type"),
        CheckConstraint("duration_minutes BETWEEN 1 AND 720", name="funnel_observation_duration"),
        CheckConstraint("slot_count IS NULL OR slot_count BETWEEN 0 AND 5000", name="funnel_observation_slots"),
        Index("ix_funnel_observation_period_master", "occurred_at", "master_id"),
        Index("ix_funnel_observation_attempt_request", "anonymous_session_hash", "request_id_hash"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    anonymous_session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id_hash: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    master_id: Mapped[int | None] = mapped_column(ForeignKey("masters.id", ondelete="SET NULL"))
    service_ids_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    slot_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(24))
    cancel_reason: Mapped[str | None] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BookingFunnelObservationReceipt(Base):
    """Reserve every accepted delivery ID, including semantic retry aliases."""
    __tablename__ = "booking_funnel_observation_receipts"
    event_id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_id: Mapped[int] = mapped_column(ForeignKey("booking_funnel_observations.id", ondelete="CASCADE"), nullable=False)
