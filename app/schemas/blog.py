from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.blog import BlogSubscriptionEventType, BlogSubscriptionStatus
from app.schemas.common import ORMModel, TimestampedResponse


class BlogSubscriptionCreate(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default="website", max_length=100)
    language: str | None = Field(default=None, max_length=16)
    referrer: str | None = Field(default=None, max_length=1000)
    utm_source: str | None = Field(default=None, max_length=255)
    utm_medium: str | None = Field(default=None, max_length=255)
    utm_campaign: str | None = Field(default=None, max_length=255)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class BlogSubscriptionUnsubscribeRequest(BaseModel):
    email: EmailStr | None = None
    token: str | None = Field(default=None, min_length=8, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_email_or_token(self) -> "BlogSubscriptionUnsubscribeRequest":
        if self.email is None and self.token is None:
            raise ValueError("email or token is required")
        return self


class BlogSubscriptionPublicResponse(BaseModel):
    email: EmailStr
    status: BlogSubscriptionStatus
    is_subscribed: bool
    subscribed_at: datetime | None
    unsubscribed_at: datetime | None
    unsubscribe_token: str


class BlogSubscriptionBackofficeResponse(TimestampedResponse):
    id: int
    email: EmailStr
    name: str | None
    status: BlogSubscriptionStatus
    source: str | None
    language: str | None
    referrer: str | None
    utm_source: str | None
    utm_medium: str | None
    utm_campaign: str | None
    first_subscribed_at: datetime
    subscribed_at: datetime
    unsubscribed_at: datetime | None
    unsubscribe_reason: str | None
    metadata_json: dict[str, Any]


class BlogSubscriptionDailyStats(BaseModel):
    date: str
    subscribed: int
    unsubscribed: int
    net_growth: int


class BlogSubscriptionSourceStats(BaseModel):
    source: str
    active_subscribers: int
    subscribe_events: int
    unsubscribe_events: int


class BlogSubscriptionLanguageStats(BaseModel):
    language: str
    active_subscribers: int


class BlogSubscriptionReasonStats(BaseModel):
    reason: str
    count: int


class BlogSubscriptionEventResponse(BaseModel):
    event_type: BlogSubscriptionEventType
    count: int


class BlogSubscriptionEventBackofficeResponse(ORMModel):
    id: int
    subscription_id: int
    event_type: BlogSubscriptionEventType
    source: str | None
    occurred_at: datetime
    metadata_json: dict[str, Any]


class BlogSubscriptionAnalyticsResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    total_subscribers: int
    active_subscribers: int
    unsubscribed_subscribers: int
    subscribe_events: int
    unsubscribe_events: int
    net_growth: int
    unsubscribe_rate: float
    events: list[BlogSubscriptionEventResponse]
    by_date: list[BlogSubscriptionDailyStats]
    by_source: list[BlogSubscriptionSourceStats]
    by_language: list[BlogSubscriptionLanguageStats]
    unsubscribe_reasons: list[BlogSubscriptionReasonStats]
