from __future__ import annotations

import enum

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class BookingFunnelEventType(str, enum.Enum):
    booking_start = "booking_start"
    service_selected = "service_selected"
    master_selected = "master_selected"
    slot_selected = "slot_selected"
    contact_entered = "contact_entered"
    booking_success = "booking_success"
    no_slot = "no_slot"
    stale_schedule = "stale_schedule"
    booking_error = "booking_error"


class BookingFunnelEventSource(str, enum.Enum):
    client = "client"
    server = "server"


class BookingFunnelEvent(TimestampMixin, Base):
    __tablename__ = "booking_funnel_events"
    __table_args__ = (
        UniqueConstraint("event_id_hash", name="uq_booking_funnel_events_event_id_hash"),
        UniqueConstraint("booking_id", name="uq_booking_funnel_events_booking_id"),
        Index("ix_booking_funnel_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_booking_funnel_events_session_type", "anonymous_session_hash", "event_type"),
        Index("ix_booking_funnel_events_master_occurred", "master_id", "occurred_at"),
        Index("ix_booking_funnel_events_type_target_date", "event_type", "target_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[BookingFunnelEventType] = mapped_column(
        Enum(BookingFunnelEventType, name="booking_funnel_event_type"),
        nullable=False,
        index=True,
    )
    source: Mapped[BookingFunnelEventSource] = mapped_column(
        Enum(BookingFunnelEventSource, name="booking_funnel_event_source"),
        nullable=False,
    )
    anonymous_session_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    )
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    master = relationship("Master", foreign_keys=[master_id])
    service = relationship("BarberService", foreign_keys=[service_id])
    booking = relationship("Booking", foreign_keys=[booking_id])


class BookingFunnelWeeklyDigest(TimestampMixin, Base):
    __tablename__ = "booking_funnel_weekly_digests"
    __table_args__ = (
        UniqueConstraint(
            "period_start",
            "period_end",
            name="uq_booking_funnel_weekly_digests_period",
        ),
        Index("ix_booking_funnel_weekly_digests_period_end", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_status: Mapped[str] = mapped_column(String(32), nullable=False)
    insight_uk: Mapped[str] = mapped_column(String(1000), nullable=False)
    recommended_action_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recommended_action_uk: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
