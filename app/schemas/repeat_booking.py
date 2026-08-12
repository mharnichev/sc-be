from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class RepeatBookingMasterContext(BaseModel):
    id: int | None
    name: str | None
    available: bool


class RepeatBookingServiceContext(BaseModel):
    id: int
    name: str
    available: bool


class RepeatBookingContext(BaseModel):
    preferred_master: RepeatBookingMasterContext
    services: list[RepeatBookingServiceContext]
    can_prefill: bool
    fallback_required: bool
    expires_at: datetime


class RepeatBookingStartResponse(BaseModel):
    status: Literal["started"] = "started"
    context: RepeatBookingContext


class RepeatBookingAnalyticsSummary(BaseModel):
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    date_from: date
    date_to: date
    offers_sent: int
    links_opened: int
    bookings_started: int
    completed_repeat_visits: int
    open_rate_percent: Decimal | None
    start_rate_percent: Decimal | None
    completion_rate_percent: Decimal | None
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    delivery_failures: int
