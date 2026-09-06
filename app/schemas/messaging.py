from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import settings

from app.models.messaging import (
    CampaignStatus,
    CampaignType,
    ConsentStatus,
    MessageChannel,
    MessageDeliveryStatus,
    MessagePurpose,
    ReviewPlatform,
)
from app.schemas.common import TimestampedResponse


class CampaignRecipient(str, enum.Enum):
    customer = "customer"
    master = "master"


class CampaignChannelStrategy(str, enum.Enum):
    single = "single"
    telegram_then_sms = "telegram_then_sms"
    sms_then_telegram = "sms_then_telegram"


class CampaignSendingWindow(BaseModel):
    model_config = {"extra": "forbid"}
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    days: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)

    @field_validator("days")
    @classmethod
    def validate_days(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("days must contain weekdays from 0 (Monday) to 6 (Sunday)")
        return sorted(set(value))

    @model_validator(mode="after")
    def nonempty_window(self) -> "CampaignSendingWindow":
        if self.start == self.end:
            raise ValueError("Sending window start and end must differ; use null for unrestricted hours")
        return self


class AudienceCriteria(BaseModel):
    all_clients: bool = False
    barber_ids: list[int] = Field(default_factory=list)
    visited_from: datetime | None = None
    visited_to: datetime | None = None
    inactive_days: int | None = Field(default=None, ge=1)
    first_time_clients: bool = False
    vip_clients: bool = False
    vip_min_total_spent: float | None = Field(default=None, ge=0)
    birthday_month: int | None = Field(default=None, ge=1, le=12)
    service_ids: list[int] = Field(default_factory=list)
    location_key: str | None = Field(default=None, max_length=100)
    limit: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def require_some_filter(self) -> "AudienceCriteria":
        if self.all_clients:
            return self
        if any(
            [
                self.barber_ids,
                self.visited_from,
                self.visited_to,
                self.inactive_days,
                self.first_time_clients,
                self.vip_clients,
                self.birthday_month,
                self.service_ids,
            ]
        ):
            return self
        raise ValueError("Audience filter must select all_clients or at least one segment")


class MessageTemplateBase(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    channel: MessageChannel = MessageChannel.telegram
    language: str | None = Field(default=None, max_length=16)
    body: str = Field(min_length=1)
    is_active: bool = True


class MessageTemplateCreate(MessageTemplateBase):
    pass


class MessageTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    channel: MessageChannel | None = None
    language: str | None = Field(default=None, max_length=16)
    body: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class MessageTemplateResponse(TimestampedResponse):
    id: int
    name: str
    channel: MessageChannel
    language: str | None
    body: str
    is_active: bool


class CampaignBase(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    type: CampaignType
    status: CampaignStatus = CampaignStatus.draft
    channel: MessageChannel = MessageChannel.telegram
    purpose: MessagePurpose = MessagePurpose.marketing
    template_id: int | None = None
    scheduled_at: datetime | None = None
    timezone: str = Field(default="Europe/Kyiv", max_length=64)
    review_delay_minutes: int | None = Field(default=None, ge=0, le=43200)
    follow_up_delay_days: int | None = Field(default=None, ge=1, le=365)
    review_platform: ReviewPlatform | None = None
    review_url: str | None = Field(default=None, max_length=1000)
    discount_code: str | None = Field(default=None, max_length=100)
    location_key: str | None = Field(default=None, max_length=100)
    recipient: CampaignRecipient = CampaignRecipient.customer
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    audience: AudienceCriteria | None = None
    segment_ids: list[int] = Field(default_factory=list, max_length=20)
    channel_strategy: CampaignChannelStrategy = CampaignChannelStrategy.single
    exclude_returned_since_snapshot: bool = False
    exclude_upcoming_booking: bool = False
    marketing_frequency_days: int = Field(default=7, ge=1, le=365)
    sms_recipients_per_minute: int = Field(default_factory=lambda: settings.sms_campaign_recipients_per_minute, ge=1, le=480)
    sending_window: CampaignSendingWindow | None = None

    @field_validator("segment_ids")
    @classmethod
    def validate_segment_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("segment_ids must contain positive IDs")
        return list(dict.fromkeys(value))

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Invalid timezone") from exc
        return value


class CampaignCreate(CampaignBase):
    pass


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    type: CampaignType | None = None
    status: CampaignStatus | None = None
    channel: MessageChannel | None = None
    purpose: MessagePurpose | None = None
    template_id: int | None = None
    scheduled_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    review_delay_minutes: int | None = Field(default=None, ge=0, le=43200)
    follow_up_delay_days: int | None = Field(default=None, ge=1, le=365)
    review_platform: ReviewPlatform | None = None
    review_url: str | None = Field(default=None, max_length=1000)
    discount_code: str | None = Field(default=None, max_length=100)
    location_key: str | None = Field(default=None, max_length=100)
    recipient: CampaignRecipient | None = None
    metadata_json: dict[str, Any] | None = None
    audience: AudienceCriteria | None = None
    segment_ids: list[int] | None = Field(default=None, max_length=20)
    channel_strategy: CampaignChannelStrategy | None = None
    exclude_returned_since_snapshot: bool | None = None
    exclude_upcoming_booking: bool | None = None
    marketing_frequency_days: int | None = Field(default=None, ge=1, le=365)
    sms_recipients_per_minute: int | None = Field(default=None, ge=1, le=480)
    sending_window: CampaignSendingWindow | None = None

    @field_validator("segment_ids")
    @classmethod
    def validate_segment_ids(cls, value: list[int] | None) -> list[int] | None:
        return CampaignBase.validate_segment_ids(value) if value is not None else value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Invalid timezone") from exc
        return value


class CampaignResponse(TimestampedResponse):
    id: int
    name: str
    type: CampaignType
    status: CampaignStatus
    channel: MessageChannel
    purpose: MessagePurpose
    template_id: int | None
    scheduled_at: datetime | None
    timezone: str
    review_delay_minutes: int | None
    follow_up_delay_days: int | None
    review_platform: ReviewPlatform | None
    review_url: str | None
    discount_code: str | None
    location_key: str | None
    recipient: CampaignRecipient = CampaignRecipient.customer
    metadata_json: dict[str, Any]
    audience: AudienceCriteria | None = None
    template_name: str | None = None
    template_body: str | None = None
    sent_count: int = 0
    failed_count: int = 0
    segment_ids: list[int] = Field(default_factory=list)
    channel_strategy: CampaignChannelStrategy = CampaignChannelStrategy.single
    exclude_returned_since_snapshot: bool = False
    exclude_upcoming_booking: bool = False
    marketing_frequency_days: int = 7
    sms_recipients_per_minute: int = Field(default_factory=lambda: settings.sms_campaign_recipients_per_minute)
    sending_window: CampaignSendingWindow | None = None

    @model_validator(mode="after")
    def load_delivery_options(self) -> "CampaignResponse":
        for key in ("segment_ids", "channel_strategy", "exclude_returned_since_snapshot", "exclude_upcoming_booking", "marketing_frequency_days", "sms_recipients_per_minute", "sending_window"):
            if key in self.metadata_json:
                value = self.metadata_json[key]
                if key == "channel_strategy":
                    value = CampaignChannelStrategy(value)
                elif key == "sending_window" and value is not None:
                    value = CampaignSendingWindow.model_validate(value)
                setattr(self, key, value)
        return self


class RenderPreviewRequest(BaseModel):
    template_id: int | None = None
    campaign_id: int | None = None
    body: str | None = Field(default=None, min_length=1)
    customer_id: int
    appointment_id: int | None = None
    extra_variables: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_template_source(self) -> "RenderPreviewRequest":
        if self.body is None and self.template_id is None and self.campaign_id is None:
            raise ValueError("body, template_id or campaign_id is required")
        return self


class RenderPreviewResponse(BaseModel):
    rendered_message: str
    variables: dict[str, str]


class StartCampaignRequest(BaseModel):
    scheduled_at: datetime | None = None


class TestMessageRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=128)
    template_id: int | None = None
    campaign_id: int | None = None
    body: str | None = Field(default=None, min_length=1)
    customer_id: int | None = None


class MessageRecipientResponse(TimestampedResponse):
    id: int
    campaign_id: int
    run_id: int | None = None
    snapshot_facts: dict[str, Any] | None = None
    send_started_at: datetime | None = None
    sms_queue_job_id: str | None = None
    customer_id: int
    appointment_id: int | None
    waitlist_request_id: int | None = None
    waitlist_offer_id: int | None = None
    channel: MessageChannel
    status: MessageDeliveryStatus
    idempotency_key: str
    scheduled_at: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    delivery_status_checked_at: datetime | None
    rendered_message: str | None
    attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    provider_message_id: str | None


class MessageLogResponse(TimestampedResponse):
    id: int
    campaign_id: int
    recipient_id: int | None
    customer_id: int
    appointment_id: int | None
    waitlist_request_id: int | None = None
    waitlist_offer_id: int | None = None
    channel: MessageChannel
    status: MessageDeliveryStatus
    provider_response: dict[str, Any] | None
    error_reason: str | None


class ClientCommunicationPreferenceUpdate(BaseModel):
    telegram_chat_id: str | None = Field(default=None, max_length=128)
    preferred_language: str | None = Field(default=None, max_length=16)
    marketing_consent: ConsentStatus | None = None
    transactional_consent: ConsentStatus | None = None
    do_not_contact: bool | None = None
    repeat_booking_opt_out: bool | None = None
    opt_out_reason: str | None = None


class MessagingAnalyticsResponse(BaseModel):
    campaign_id: int | None = None
    total_recipients: int
    sent_count: int
    delivered_count: int
    failed_count: int
    skipped_count: int
    pending_count: int
    delivery_rate: float
    review_request_sent_count: int
    performance_by_date: list[dict[str, Any]]
    failed_messages: list[dict[str, Any]]
