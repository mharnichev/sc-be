"""Immutable recipient entitlement snapshots and privacy-safe engagement events."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base, TimestampMixin


class CampaignOffer(TimestampMixin, Base):
    __tablename__ = "campaign_offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="RESTRICT"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("campaign_runs.id", ondelete="RESTRICT"), index=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("message_recipients.id", ondelete="RESTRICT"), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="RESTRICT"), index=True)
    promotion_id: Mapped[int] = mapped_column(ForeignKey("promotions.id", ondelete="RESTRICT"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    allowed_service_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    discount_percent: Mapped[int] = mapped_column(Integer, default=30)
    promotion_name_uk: Mapped[str] = mapped_column(String(255))
    promotion_name_en: Mapped[str] = mapped_column(String(255))
    conditions_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    consumed_booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="RESTRICT"), unique=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeemed_discount_amount: Mapped[int | None] = mapped_column(Integer)
    redeemed_revenue_amount: Mapped[int | None] = mapped_column(Integer)
    raw_link_requests: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_open_count: Mapped[int] = mapped_column(Integer, default=0)
    first_confirmed_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignOfferOpen(TimestampMixin, Base):
    __tablename__ = "campaign_offer_opens"
    __table_args__ = (UniqueConstraint("offer_id", "event_hash", name="uq_campaign_offer_open_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("campaign_offers.id", ondelete="CASCADE"), index=True)
    event_hash: Mapped[str] = mapped_column(String(64))
