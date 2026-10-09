from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class PublicBookingRecoveryEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    anonymous_session_id: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        validation_alias=AliasChoices("anonymous_session_id", "anonymousSessionId"),
    )
    event_type: Literal["alternative_slot_viewed", "alternative_slot_selected", "waitlist_opened"]
    search_context_id: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$",
        validation_alias=AliasChoices("search_context_id", "searchContextId"),
    )
    offer_id: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$",
        validation_alias=AliasChoices("offer_id", "offerId"),
    )
    master_id: int | None = Field(default=None, ge=1)
    service_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def viewed_event_needs_context_and_offer(self) -> "PublicBookingRecoveryEventCreate":
        if self.event_type == "alternative_slot_viewed" and (
            self.search_context_id is None or self.offer_id is None
        ):
            raise ValueError("search_context_id and offer_id are required for alternative_slot_viewed")
        return self


class BookingRecoveryEventReceipt(BaseModel):
    event_id: str
    status: Literal["recorded", "duplicate", "unavailable"]


class BookingRecoveryStrictConversion(BaseModel):
    calculation_version: Literal[1] = 1
    period_basis: Literal["alternatives_requested_at"] = "alternatives_requested_at"
    requested_sessions: int = Field(ge=0)
    viewed_sessions: int = Field(ge=0)
    selected_sessions: int = Field(ge=0)
    completed_sessions: int = Field(ge=0)
    selection_rate_percent: Decimal | None
    completion_rate_percent: Decimal | None
    follow_up_window: Literal["observed_until_summary_at"] = "observed_until_summary_at"
    follow_up_observed_until: datetime
    definition: str = (
        "Distinct sessions with requests in the report period. Views, selections and completions "
        "must occur strictly after each preceding step, up to summary observation time, in the same "
        "session and search context; selections and completions must match the viewed offer."
    )


class BookingRecoverySummary(BaseModel):
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    date_from: date
    date_to: date
    no_slot_sessions: int
    alternatives_requested: int
    alternatives_returned: int
    alternative_slots_returned: int
    alternative_slots_viewed: int
    alternative_slots_selected: int
    bookings_after_alternative: int
    alternative_recovery_rate_percent: Decimal | None
    alternative_selection_rate_percent: Decimal | None
    alternative_cohort_requested_sessions: int
    alternative_cohort_viewed_sessions: int
    alternative_cohort_selected_sessions: int
    alternative_cohort_completed_sessions: int
    alternative_cohort_selection_rate_percent: Decimal | None
    alternative_cohort_completion_rate_percent: Decimal | None
    alternative_strict_conversion: BookingRecoveryStrictConversion
    alternative_engagement_collection_started_at: datetime | None
    waitlist_requests: int
    offers_sent: int
    offers_delivered: int
    offers_claimed: int
    offers_expired: int
    cancelled_slots_refilled: int
    average_cancellation_to_refill_seconds: int | None

    @field_validator(
        "alternative_recovery_rate_percent",
        "alternative_selection_rate_percent",
        "alternative_cohort_selection_rate_percent",
        "alternative_cohort_completion_rate_percent",
    )
    @classmethod
    def clamp_percentage(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return min(Decimal("100.00"), max(Decimal("0.00"), value))
