from __future__ import annotations

import enum

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class CampaignType(str, enum.Enum):
    manual = "manual"
    booking_confirmation = "booking_confirmation"
    post_visit_review_request = "post_visit_review_request"
    appointment_reminder = "appointment_reminder"
    birthday_greeting = "birthday_greeting"
    re_engagement = "re_engagement"
    first_visit_follow_up = "first_visit_follow_up"
    loyalty_vip = "loyalty_vip"


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    completed = "completed"
    archived = "archived"


class MessageChannel(str, enum.Enum):
    telegram = "telegram"
    sms = "sms"
    whatsapp = "whatsapp"
    email = "email"


class MessageDeliveryStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class MessagePurpose(str, enum.Enum):
    marketing = "marketing"
    transactional = "transactional"
    review_request = "review_request"


class ConsentStatus(str, enum.Enum):
    unknown = "unknown"
    opted_in = "opted_in"
    opted_out = "opted_out"


class ReviewPlatform(str, enum.Enum):
    google = "google"
    instagram = "instagram"
    internal = "internal"
    custom = "custom"


class ReviewRequestStatus(str, enum.Enum):
    scheduled = "scheduled"
    sent = "sent"
    delivered = "delivered"
    submitted = "submitted"
    expired = "expired"
    failed = "failed"


class MessageTemplate(TimestampMixin, Base):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    channel: Mapped[MessageChannel] = mapped_column(Enum(MessageChannel), default=MessageChannel.telegram, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    campaigns = relationship("Campaign", back_populates="template")


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[CampaignType] = mapped_column(Enum(CampaignType), nullable=False, index=True)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus),
        default=CampaignStatus.draft,
        nullable=False,
        index=True,
    )
    channel: Mapped[MessageChannel] = mapped_column(Enum(MessageChannel), default=MessageChannel.telegram, nullable=False)
    purpose: Mapped[MessagePurpose] = mapped_column(Enum(MessagePurpose), default=MessagePurpose.marketing, nullable=False)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_templates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Kyiv", nullable=False)
    review_delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    follow_up_delay_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_platform: Mapped[ReviewPlatform | None] = mapped_column(Enum(ReviewPlatform), nullable=True)
    review_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    discount_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    location_key: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    template = relationship("MessageTemplate", back_populates="campaigns")
    audience_filter = relationship(
        "CampaignAudienceFilter",
        back_populates="campaign",
        uselist=False,
        cascade="all, delete-orphan",
    )
    recipients = relationship("MessageRecipient", back_populates="campaign", cascade="all, delete-orphan")
    review_requests = relationship("ReviewRequest", back_populates="campaign")


class CampaignAudienceFilter(TimestampMixin, Base):
    __tablename__ = "campaign_audience_filters"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    campaign = relationship("Campaign", back_populates="audience_filter")


class ClientCommunicationPreference(TimestampMixin, Base):
    __tablename__ = "client_communication_preferences"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    telegram_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    preferred_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    marketing_consent: Mapped[ConsentStatus] = mapped_column(
        Enum(ConsentStatus),
        default=ConsentStatus.unknown,
        nullable=False,
    )
    transactional_consent: Mapped[ConsentStatus] = mapped_column(
        Enum(ConsentStatus),
        default=ConsentStatus.opted_in,
        nullable=False,
    )
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    blacklisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_out_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer = relationship("Customer")


class TelegramContact(TimestampMixin, Base):
    __tablename__ = "telegram_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    linked_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_update_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    raw_update: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    linked_customer = relationship("Customer")


class TelegramBotSession(TimestampMixin, Base):
    __tablename__ = "telegram_bot_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    telegram_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    linked_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    selected_master_id: Mapped[int | None] = mapped_column(
        ForeignKey("masters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    selected_service_id: Mapped[int | None] = mapped_column(
        ForeignKey("barber_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(100), default="idle", nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    telegram_contact = relationship("TelegramContact")
    linked_customer = relationship("Customer")
    selected_master = relationship("Master", foreign_keys=[selected_master_id])
    selected_service = relationship("BarberService", foreign_keys=[selected_service_id])


class MessageRecipient(TimestampMixin, Base):
    __tablename__ = "message_recipients"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_message_recipients_idempotency_key"),
        Index("ix_message_recipients_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, index=True)
    channel: Mapped[MessageChannel] = mapped_column(Enum(MessageChannel), default=MessageChannel.telegram, nullable=False)
    status: Mapped[MessageDeliveryStatus] = mapped_column(
        Enum(MessageDeliveryStatus),
        default=MessageDeliveryStatus.pending,
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rendered_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    campaign = relationship("Campaign", back_populates="recipients")
    customer = relationship("Customer")
    appointment = relationship("Booking")
    logs = relationship("MessageLog", back_populates="recipient", cascade="all, delete-orphan")


class MessageLog(TimestampMixin, Base):
    __tablename__ = "message_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_recipients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, index=True)
    channel: Mapped[MessageChannel] = mapped_column(Enum(MessageChannel), nullable=False)
    status: Mapped[MessageDeliveryStatus] = mapped_column(Enum(MessageDeliveryStatus), nullable=False, index=True)
    provider_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipient = relationship("MessageRecipient", back_populates="logs")
    customer = relationship("Customer")
    campaign = relationship("Campaign")


class ReviewRequest(TimestampMixin, Base):
    __tablename__ = "review_requests"
    __table_args__ = (
        UniqueConstraint("campaign_id", "appointment_id", name="uq_review_requests_campaign_appointment"),
        UniqueConstraint("appointment_id", name="uq_review_requests_appointment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    review_id: Mapped[int | None] = mapped_column(
        ForeignKey("master_reviews.id", ondelete="SET NULL"), unique=True, nullable=True, index=True
    )
    platform: Mapped[ReviewPlatform] = mapped_column(Enum(ReviewPlatform), nullable=False)
    review_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    follow_up_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("message_recipients.id", ondelete="SET NULL"), nullable=True)
    channel: Mapped[MessageChannel] = mapped_column(
        Enum(MessageChannel), default=MessageChannel.sms, nullable=False
    )
    fallback_channel: Mapped[MessageChannel | None] = mapped_column(Enum(MessageChannel), nullable=True)
    status: Mapped[ReviewRequestStatus] = mapped_column(
        Enum(ReviewRequestStatus), default=ReviewRequestStatus.scheduled, nullable=False, index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    campaign = relationship("Campaign", back_populates="review_requests")
    appointment = relationship("Booking")
    customer = relationship("Customer")
    master = relationship("Master")
    review = relationship("MasterReview")
    recipient = relationship("MessageRecipient")
    events = relationship(
        "ReviewRequestEvent",
        back_populates="review_request",
        cascade="all, delete-orphan",
        order_by="ReviewRequestEvent.created_at",
    )


class ReviewRequestEvent(TimestampMixin, Base):
    __tablename__ = "review_request_events"
    __table_args__ = (Index("ix_review_request_events_request_created", "review_request_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    review_request_id: Mapped[int] = mapped_column(
        ForeignKey("review_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[ReviewRequestStatus] = mapped_column(Enum(ReviewRequestStatus), nullable=False, index=True)
    channel: Mapped[MessageChannel | None] = mapped_column(Enum(MessageChannel), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    review_request = relationship("ReviewRequest", back_populates="events")


class ChannelProviderConfig(TimestampMixin, Base):
    __tablename__ = "channel_provider_configs"
    __table_args__ = (UniqueConstraint("channel", "provider_name", name="uq_channel_provider_configs_channel_provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[MessageChannel] = mapped_column(Enum(MessageChannel), nullable=False, index=True)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
