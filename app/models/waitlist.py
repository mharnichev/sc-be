from __future__ import annotations

import enum
from datetime import date, datetime, time
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Table, Time, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class WaitlistStatus(str, enum.Enum):
    active = "active"
    offered = "offered"
    booked = "booked"
    expired = "expired"
    cancelled = "cancelled"


class WaitlistOfferStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    delivered = "delivered"
    claimed = "claimed"
    expired = "expired"
    cancelled = "cancelled"


waitlist_request_services = Table(
    "waitlist_request_services",
    Base.metadata,
    Column("waitlist_request_id", ForeignKey("waitlist_requests.id", ondelete="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey("barber_services.id", ondelete="RESTRICT"), primary_key=True),
)


class WaitlistRequest(TimestampMixin, Base):
    __tablename__ = "waitlist_requests"
    __table_args__ = (
        Index("ix_waitlist_requests_matching", "status", "desired_date", "preferred_master_id", "expires_at"),
        Index(
            "uq_waitlist_requests_open_dedup_key",
            "dedup_key_hash",
            unique=True,
            postgresql_where=text("status IN ('active', 'offered')"),
            sqlite_where=text("status IN ('active', 'offered')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=lambda: str(uuid4()))
    cancel_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    dedup_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    preferred_master_id: Mapped[int | None] = mapped_column(ForeignKey("masters.id", ondelete="SET NULL"), index=True)
    desired_date: Mapped[date] = mapped_column(Date, index=True)
    acceptable_date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    acceptable_date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    preferred_time_from: Mapped[time | None] = mapped_column(Time, nullable=True)
    preferred_time_to: Mapped[time | None] = mapped_column(Time, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    notification_consent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[WaitlistStatus] = mapped_column(Enum(WaitlistStatus), default=WaitlistStatus.active, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(255))

    customer = relationship("Customer")
    preferred_master = relationship("Master", foreign_keys=[preferred_master_id])
    services = relationship("BarberService", secondary=waitlist_request_services)
    offers = relationship("WaitlistOffer", back_populates="request", cascade="all, delete-orphan")


class WaitlistOffer(TimestampMixin, Base):
    __tablename__ = "waitlist_offers"
    __table_args__ = (
        UniqueConstraint("request_id", "master_id", "start_at", name="uq_waitlist_offers_request_slot"),
        Index("ix_waitlist_offers_slot", "master_id", "start_at", "status"),
        CheckConstraint("end_at > start_at", name="waitlist_offer_positive_interval"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("waitlist_requests.id", ondelete="CASCADE"), index=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="RESTRICT"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[WaitlistOfferStatus] = mapped_column(Enum(WaitlistOfferStatus), default=WaitlistOfferStatus.pending, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(255))
    provider_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    source_booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"), index=True)

    request = relationship("WaitlistRequest", back_populates="offers")
    master = relationship("Master")
    source_booking = relationship("Booking")
