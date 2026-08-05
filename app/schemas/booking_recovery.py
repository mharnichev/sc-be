from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class PublicBookingRecoveryEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    anonymous_session_id: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        validation_alias=AliasChoices("anonymous_session_id", "anonymousSessionId"),
    )
    event_type: Literal["alternative_slot_selected", "waitlist_opened"]
    master_id: int | None = Field(default=None, ge=1)
    service_id: int | None = Field(default=None, ge=1)


class BookingRecoveryEventReceipt(BaseModel):
    event_id: str
    status: Literal["recorded", "duplicate"]


class BookingRecoverySummary(BaseModel):
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    date_from: date
    date_to: date
    no_slot_sessions: int
    alternatives_requested: int
    alternatives_returned: int
    alternative_slots_returned: int
    alternative_slots_selected: int
    bookings_after_alternative: int
    alternative_recovery_rate_percent: Decimal | None
    waitlist_requests: int
    offers_sent: int
    offers_delivered: int
    offers_claimed: int
    offers_expired: int
    cancelled_slots_refilled: int
    average_cancellation_to_refill_seconds: int | None

    @field_validator("alternative_recovery_rate_percent")
    @classmethod
    def clamp_percentage(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return min(Decimal("100.00"), max(Decimal("0.00"), value))
