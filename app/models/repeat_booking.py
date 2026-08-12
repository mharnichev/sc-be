from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class RepeatBookingOfferStatus(str, enum.Enum):
    scheduled = "scheduled"
    sent = "sent"
    opened = "opened"
    started = "started"
    booked = "booked"
    expired = "expired"
    skipped = "skipped"
    failed = "failed"


class RepeatBookingEventType(str, enum.Enum):
    offer_scheduled = "repeat_offer_scheduled"
    offer_sent = "repeat_offer_sent"
    offer_delivery_failed = "repeat_offer_delivery_failed"
    link_opened = "repeat_link_opened"
    booking_started = "repeat_booking_started"
    booking_completed = "repeat_booking_completed"
    offer_expired = "repeat_offer_expired"
    offer_skipped = "repeat_offer_skipped"


class RepeatBookingOffer(TimestampMixin, Base):
    """Hash-only, single-purpose capability and delivery state for one completed visit."""

    __tablename__ = "repeat_booking_offers"
    __table_args__ = (
        UniqueConstraint("completed_booking_id", name="uq_repeat_booking_offers_completed_booking_id"),
        UniqueConstraint("token_hash", name="uq_repeat_booking_offers_token_hash"),
        UniqueConstraint("result_booking_id", name="uq_repeat_booking_offers_result_booking_id"),
        Index("ix_repeat_booking_offers_due", "status", "scheduled_at"),
        Index("ix_repeat_booking_offers_expiry", "status", "expires_at"),
        Index("ix_repeat_booking_offers_customer_sent", "customer_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    completed_booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    preferred_master_id: Mapped[int | None] = mapped_column(
        ForeignKey("masters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    service_ids: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[RepeatBookingOfferStatus] = mapped_column(
        Enum(RepeatBookingOfferStatus),
        default=RepeatBookingOfferStatus.scheduled,
        nullable=False,
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )

    completed_booking = relationship("Booking", foreign_keys=[completed_booking_id])
    result_booking = relationship("Booking", foreign_keys=[result_booking_id])
    customer = relationship("Customer")
    preferred_master = relationship("Master")
    events = relationship(
        "RepeatBookingEvent",
        back_populates="offer",
        cascade="all, delete-orphan",
        order_by="RepeatBookingEvent.created_at",
    )


class RepeatBookingEvent(TimestampMixin, Base):
    """Privacy-safe and idempotent repeat-booking lifecycle event."""

    __tablename__ = "repeat_booking_events"
    __table_args__ = (
        UniqueConstraint("event_key_hash", name="uq_repeat_booking_events_event_key_hash"),
        Index("ix_repeat_booking_events_created_type", "created_at", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(
        ForeignKey("repeat_booking_offers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    offer = relationship("RepeatBookingOffer", back_populates="events")
