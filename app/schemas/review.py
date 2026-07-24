from __future__ import annotations

from datetime import date, datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings
from app.models.master_review import MasterReviewStatus
from app.models.messaging import MessageChannel, ReviewRequestStatus


class GoogleBusinessReviewer(BaseModel):
    display_name: str | None = None
    profile_photo_url: str | None = None
    is_anonymous: bool = False


class GoogleBusinessReviewReply(BaseModel):
    comment: str | None = None
    update_time: datetime | None = None


class GoogleBusinessReview(BaseModel):
    review_id: str
    name: str | None = None
    reviewer: GoogleBusinessReviewer | None = None
    star_rating: int | None = None
    comment: str | None = None
    translations: dict[str, str] | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None
    review_reply: GoogleBusinessReviewReply | None = None


class GoogleBusinessReviewsResponse(BaseModel):
    average_rating: float | None = None
    total_review_count: int = 0
    fetched_at: datetime | None = None
    cache_expires_at: datetime | None = None
    stale: bool = False
    items: list[GoogleBusinessReview]


class ReviewSubmission(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=settings.review_comment_max_length)

    @field_validator("rating", mode="before")
    @classmethod
    def rating_must_be_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rating must be an integer from 1 to 5")
        return value


class PublicReviewRequestContext(BaseModel):
    state: Literal["available", "submitted"]
    master_id: int
    master_name: str
    master_photo_url: str | None = None
    visit_date: datetime
    service_names: list[str]
    expires_at: datetime


class ReviewSubmissionResponse(BaseModel):
    status: Literal["pending"] = "pending"
    submitted_at: datetime


class MasterRatingSummary(BaseModel):
    master_id: int
    average_rating: float | None = None
    approved_review_count: int = 0
    pending_review_count: int = 0
    rating_distribution: dict[int, int] = Field(default_factory=dict)


class PublicMasterReview(BaseModel):
    id: int
    rating: int
    comment: str | None
    author_name: str
    published_at: datetime


class PublicMasterReviewsResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PublicMasterReview]


class ReviewRequestEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: ReviewRequestStatus
    state: ReviewRequestStatus
    channel: MessageChannel | None
    reason: str | None
    failure_reason: str | None
    created_at: datetime
    occurred_at: datetime


class ModerationAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_status: MasterReviewStatus
    to_status: MasterReviewStatus
    action: MasterReviewStatus
    actor_id: int | None
    actor_display_name: str | None
    reason: str | None
    created_at: datetime
    occurred_at: datetime


class ReviewMasterContext(BaseModel):
    id: int
    full_name: str
    full_name_uk: str
    full_name_en: str | None = None
    first_name_uk: str
    last_name_uk: str | None = None


class MasterRatingStatistics(BaseModel):
    master_id: int
    master: ReviewMasterContext | None = None
    approved_average_rating: float | None = None
    approved_review_count: int = 0
    pending_review_count: int = 0
    rating_distribution: dict[int, int] = Field(default_factory=dict)


class AdminMasterReviewListItem(BaseModel):
    id: int
    booking_reference: str
    master_id: int
    master_name: str
    master: ReviewMasterContext
    customer_display_name: str
    rating: int
    comment: str | None
    text: str | None
    status: MasterReviewStatus
    moderation_status: MasterReviewStatus
    submitted_at: datetime
    moderated_at: datetime | None
    request_status: ReviewRequestStatus
    request_state: ReviewRequestStatus
    request_channel: MessageChannel
    requested_at: datetime | None


class AdminMasterReviewDetail(AdminMasterReviewListItem):
    moderation_reason: str | None
    published_at: datetime | None
    request_scheduled_at: datetime | None
    request_sent_at: datetime | None
    request_delivered_at: datetime | None
    request_expires_at: datetime | None
    request_failure_reason: str | None
    moderation_history: list[ModerationAuditResponse]
    request_history: list[ReviewRequestEventResponse]


class AdminMasterReviewsResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[AdminMasterReviewListItem]


class ReviewModerationRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ReviewAutomationSettings(BaseModel):
    enabled: bool
    delay_minutes: int = Field(ge=0, le=10080)
    primary_channel: MessageChannel = MessageChannel.telegram
    fallback_channel: MessageChannel | None = MessageChannel.sms
    quiet_hours_from: str = "20:00"
    quiet_hours_to: str = "10:00"
    frequency_cap_days: int = Field(default=90, ge=0, le=365)
    submitted_frequency_cap_days: int = Field(default=270, ge=0, le=365)
    exclusions: dict[str, object] = Field(default_factory=dict)
    template_preview: str | None = None

    @field_validator("quiet_hours_from", "quiet_hours_to")
    @classmethod
    def validate_quiet_hours(cls, value: str) -> str:
        try:
            hour_text, minute_text = value.split(":", maxsplit=1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("quiet hours must use HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("quiet hours must use HH:MM")
        return f"{hour:02d}:{minute:02d}"


class ReviewAutomationSettingsUpdate(BaseModel):
    enabled: bool | None = None
    delay_minutes: int | None = Field(default=None, ge=0, le=10080)
    primary_channel: MessageChannel | None = None
    fallback_channel: MessageChannel | None = None
    quiet_hours_from: str | None = None
    quiet_hours_to: str | None = None
    frequency_cap_days: int | None = Field(default=None, ge=0, le=365)
    submitted_frequency_cap_days: int | None = Field(default=None, ge=0, le=365)
    exclusions: dict[str, object] | None = None

    @field_validator("quiet_hours_from", "quiet_hours_to")
    @classmethod
    def validate_optional_quiet_hours(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ReviewAutomationSettings.validate_quiet_hours(value)


class ReviewRequestSettings(BaseModel):
    enabled: bool
    delay_minutes: int = Field(ge=0, le=10080)
    primary_channel: Literal["sms"] = "sms"
    sms_fallback_enabled: Literal[False] = False
    quiet_hours_enabled: bool = True
    quiet_hours_from: str = "20:00"
    quiet_hours_to: str = "10:00"
    frequency_cap_count: Literal[1] = 1
    frequency_cap_days: int = Field(default=90, ge=1, le=365)
    submitted_frequency_cap_days: int = Field(default=270, ge=1, le=365)
    exclusions: list[str] = Field(default_factory=list)
    template_preview: str = ""
    updated_at: datetime | None = None

    @field_validator("quiet_hours_from", "quiet_hours_to")
    @classmethod
    def validate_quiet_hours(cls, value: str) -> str:
        return ReviewAutomationSettings.validate_quiet_hours(value)


class ReviewRequestSettingsUpdate(BaseModel):
    enabled: bool
    delay_minutes: int = Field(ge=0, le=10080)
    primary_channel: Literal["sms"] = "sms"
    sms_fallback_enabled: Literal[False] = False
    quiet_hours_enabled: bool
    quiet_hours_from: str
    quiet_hours_to: str
    frequency_cap_count: Literal[1] = 1
    frequency_cap_days: int = Field(ge=1, le=365)
    submitted_frequency_cap_days: int = Field(default=270, ge=1, le=365)
    exclusions: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("quiet_hours_from", "quiet_hours_to")
    @classmethod
    def validate_quiet_hours(cls, value: str) -> str:
        return ReviewAutomationSettings.validate_quiet_hours(value)


class ReviewMetricsResponse(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    cohort_definition: str
    eligible_completed_visits: int
    scheduled: int
    sent: int
    delivered: int
    submitted: int
    expired: int
    failed: int
    approved: int
    conversion_rate: float
    average_approved_rating: float | None
    moderation_time_hours: float | None
    low_rating_pending_count: int
    requests_scheduled: int
    requests_sent: int
    requests_delivered: int
    review_form_opens: int | None = Field(
        default=None,
        description="Unavailable until review-form opens are persisted as domain events.",
    )
    submitted_reviews: int
    approved_reviews: int
    review_conversion_rate: float
    average_moderation_time_minutes: float | None
    average_rating_by_master: list[MasterRatingStatistics] = Field(default_factory=list)
