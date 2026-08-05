from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class BookingRecoveryEventType(str, enum.Enum):
    alternatives_requested = "alternatives_requested"
    alternatives_returned = "alternatives_returned"
    alternative_slot_selected = "alternative_slot_selected"
    waitlist_opened = "waitlist_opened"
    waitlist_submitted = "waitlist_submitted"
    waitlist_offer_sent = "waitlist_offer_sent"
    waitlist_offer_delivered = "waitlist_offer_delivered"
    waitlist_offer_claimed = "waitlist_offer_claimed"
    waitlist_offer_expired = "waitlist_offer_expired"
    booking_completed_after_alternative = "booking_completed_after_alternative"
    booking_completed_after_waitlist_offer = "booking_completed_after_waitlist_offer"


class BookingRecoveryEvent(TimestampMixin, Base):
    """Privacy-safe, idempotent operational events for no-slot recovery."""

    __tablename__ = "booking_recovery_events"
    __table_args__ = (
        UniqueConstraint("event_key_hash", name="uq_booking_recovery_events_event_key_hash"),
        Index("ix_booking_recovery_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_booking_recovery_events_session_type", "anonymous_session_hash", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    anonymous_session_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    master_id: Mapped[int | None] = mapped_column(
        ForeignKey("masters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("barber_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    waitlist_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("waitlist_requests.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    waitlist_offer_id: Mapped[int | None] = mapped_column(
        ForeignKey("waitlist_offers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    metric_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    master = relationship("Master", foreign_keys=[master_id])
    service = relationship("BarberService", foreign_keys=[service_id])
    booking = relationship("Booking", foreign_keys=[booking_id])
    source_booking = relationship("Booking", foreign_keys=[source_booking_id])
    waitlist_request = relationship("WaitlistRequest", foreign_keys=[waitlist_request_id])
    waitlist_offer = relationship("WaitlistOffer", foreign_keys=[waitlist_offer_id])
