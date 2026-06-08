from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class BlogSubscriptionStatus(str, enum.Enum):
    subscribed = "subscribed"
    unsubscribed = "unsubscribed"


class BlogSubscriptionEventType(str, enum.Enum):
    subscribed = "subscribed"
    resubscribed = "resubscribed"
    unsubscribed = "unsubscribed"


class BlogSubscription(TimestampMixin, Base):
    __tablename__ = "blog_subscriptions"
    __table_args__ = (
        UniqueConstraint("email", name="uq_blog_subscriptions_email"),
        UniqueConstraint("unsubscribe_token", name="uq_blog_subscriptions_unsubscribe_token"),
        Index("ix_blog_subscriptions_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[BlogSubscriptionStatus] = mapped_column(
        Enum(BlogSubscriptionStatus),
        default=BlogSubscriptionStatus.subscribed,
        nullable=False,
        index=True,
    )
    source: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    referrer: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    utm_source: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    utm_medium: Mapped[str | None] = mapped_column(String(255), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    unsubscribe_token: Mapped[str] = mapped_column(String(128), nullable=False)
    first_subscribed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    subscribed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    unsubscribe_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    subscriber_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    events = relationship("BlogSubscriptionEvent", back_populates="subscription", cascade="all, delete-orphan")


class BlogSubscriptionEvent(Base):
    __tablename__ = "blog_subscription_events"
    __table_args__ = (
        Index("ix_blog_subscription_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_blog_subscription_events_source_occurred", "source", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("blog_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[BlogSubscriptionEventType] = mapped_column(
        Enum(BlogSubscriptionEventType),
        nullable=False,
        index=True,
    )
    source: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    subscriber_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    subscription = relationship("BlogSubscription", back_populates="events")
