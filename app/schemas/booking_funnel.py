from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.booking_funnel import BookingFunnelEventType


FUNNEL_STEP_TYPES = (
    BookingFunnelEventType.booking_start,
    BookingFunnelEventType.service_selected,
    BookingFunnelEventType.master_selected,
    BookingFunnelEventType.slot_selected,
    BookingFunnelEventType.contact_entered,
    BookingFunnelEventType.booking_success,
)
CLIENT_EVENT_TYPES = frozenset(
    {
        BookingFunnelEventType.booking_start,
        BookingFunnelEventType.service_selected,
        BookingFunnelEventType.master_selected,
        BookingFunnelEventType.slot_selected,
        BookingFunnelEventType.contact_entered,
        BookingFunnelEventType.no_slot,
        BookingFunnelEventType.stale_schedule,
        BookingFunnelEventType.booking_error,
    }
)


class PublicBookingFunnelEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    anonymous_session_id: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        validation_alias=AliasChoices("anonymous_session_id", "anonymousSessionId"),
    )
    event_type: BookingFunnelEventType
    master_id: int | None = Field(default=None, ge=1)
    service_id: int | None = Field(default=None, ge=1)
    service_ids: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="Complete selected service set for a no_slot observation.",
        examples=[[11, 12]],
    )
    target_date: date | None = Field(
        default=None,
        description="Europe/Kyiv calendar date searched by the visitor for a no_slot event.",
        examples=["2026-08-08"],
    )
    duration_minutes: int | None = Field(
        default=None,
        gt=0,
        le=720,
        description="Total requested booking duration for a no_slot observation.",
        examples=[90],
    )

    @field_validator("service_ids")
    @classmethod
    def normalize_service_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(service_id < 1 for service_id in value):
            raise ValueError("service_ids must contain positive integers")
        return sorted(set(value))

    @field_validator("event_type")
    @classmethod
    def public_event_must_be_client_owned(
        cls,
        value: BookingFunnelEventType,
    ) -> BookingFunnelEventType:
        if value not in CLIENT_EVENT_TYPES:
            raise ValueError("booking_success is recorded by the server after booking creation")
        return value

    @model_validator(mode="after")
    def target_date_belongs_to_no_slot(self) -> "PublicBookingFunnelEventCreate":
        if self.target_date is not None and self.event_type != BookingFunnelEventType.no_slot:
            raise ValueError("target_date is supported only for no_slot events")
        if self.service_ids is not None and self.event_type != BookingFunnelEventType.no_slot:
            raise ValueError("service_ids is supported only for no_slot events")
        if self.duration_minutes is not None and self.event_type != BookingFunnelEventType.no_slot:
            raise ValueError("duration_minutes is supported only for no_slot events")
        if self.event_type == BookingFunnelEventType.no_slot:
            if self.master_id is None:
                raise ValueError("master_id is required for no_slot events")
            if self.service_id is None and self.service_ids is None:
                raise ValueError("service_id or service_ids is required for no_slot events")
            if self.target_date is None:
                raise ValueError("target_date is required for no_slot events")
            if self.duration_minutes is None:
                raise ValueError("duration_minutes is required for no_slot events")
        if self.service_ids is not None:
            if self.master_id is None:
                raise ValueError("master_id is required when service_ids is provided")
            if self.service_id is not None and self.service_id not in self.service_ids:
                raise ValueError("service_id must be included in service_ids")
            if self.service_id is None:
                self.service_id = self.service_ids[0]
        return self


class BookingFunnelEventReceipt(BaseModel):
    event_id: str
    status: Literal["recorded", "duplicate"]


class BookingFunnelStepMetric(BaseModel):
    event_type: BookingFunnelEventType
    count: int


class BookingFunnelConversionMetric(BaseModel):
    from_step: BookingFunnelEventType
    to_step: BookingFunnelEventType
    from_count: int
    to_count: int
    conversion_percent: Decimal | None = Field(default=None, max_digits=7, decimal_places=2)
    status: Literal["available", "unavailable"]
    unavailable_reason: str | None = None


class BookingFunnelDropOffMetric(BaseModel):
    from_step: BookingFunnelEventType
    to_step: BookingFunnelEventType
    count: int | None
    drop_off_percent: Decimal | None = Field(default=None, max_digits=7, decimal_places=2)
    status: Literal["available", "unavailable"]


class BookingFunnelOverallConversion(BaseModel):
    started: int
    succeeded: int
    conversion_percent: Decimal | None = Field(default=None, max_digits=7, decimal_places=2)
    status: Literal["available", "unavailable"]
    unavailable_reason: str | None = None


class BookingFunnelAlertThresholds(BaseModel):
    no_slot_min_count: int
    no_slot_rate_percent: Decimal = Field(max_digits=7, decimal_places=2)
    stale_schedule_count: int
    booking_error_count: int
    meaningful_step_sessions: int


class BookingFunnelOperationalAlert(BaseModel):
    code: Literal["no_slot", "stale_schedule", "booking_error"]
    count: int
    rate_percent: Decimal | None = Field(default=None, max_digits=7, decimal_places=2)
    triggered: bool


class BookingFunnelNoSlotDateMetric(BaseModel):
    target_date: date
    observations: int
    unique_sessions: int
    affected_masters: int
    first_observed_at: datetime
    last_observed_at: datetime


class BookingFunnelNoSlotServiceRef(BaseModel):
    service_id: int
    service_name: str | None


class BookingFunnelNoSlotContextMetric(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "target_date": "2026-08-08",
                    "master_id": 7,
                    "master_name": "Андрій",
                    "services": [
                        {"service_id": 11, "service_name": "Стрижка"},
                        {"service_id": 12, "service_name": "Борода"},
                    ],
                    "duration_minutes": 90,
                    "observations": 4,
                    "unique_sessions": 3,
                    "first_observed_at": "2026-08-02T10:15:00+03:00",
                    "last_observed_at": "2026-08-02T18:40:00+03:00",
                }
            ]
        }
    )

    target_date: date
    master_id: int | None
    master_name: str | None
    services: list[BookingFunnelNoSlotServiceRef]
    duration_minutes: int | None
    observations: int
    unique_sessions: int
    first_observed_at: datetime
    last_observed_at: datetime


class BookingFunnelRecommendedAction(BaseModel):
    code: Literal[
        "review_availability",
        "refresh_schedule",
        "investigate_booking_errors",
        "improve_service_discovery",
        "clarify_master_choice",
        "simplify_contact_step",
        "investigate_booking_completion",
    ]
    title_uk: str
    explanation_uk: str
    recommended_backoffice_route: str
    based_on: str


class BookingFunnelWeeklyDigestResponse(BaseModel):
    scope: Literal["all_masters"] = "all_masters"
    period_start: date
    period_end: date
    generated_at: datetime
    status: Literal["available", "partial", "empty", "unavailable"]
    insight_uk: str
    recommended_action: BookingFunnelRecommendedAction | None
    step_counts: list[BookingFunnelStepMetric]
    operational_alerts: list[BookingFunnelOperationalAlert]


class BookingFunnelAggregate(BaseModel):
    calculation_version: Literal[2] = 2
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    cohort_definition: str = (
        "Anonymous booking attempts whose earliest booking_start was recorded in the selected period; "
        "later steps are matched by the same anonymous session."
    )
    master_attribution_definition: str = (
        "Early unscoped steps are attributed to every master selected later in the same attempt; "
        "master funnels are therefore not additive."
    )
    status: Literal["available", "partial", "empty", "unavailable"]
    status_reason: str | None = None
    tracking_gap_count: int = 0
    steps: list[BookingFunnelStepMetric]
    step_to_step_conversion: list[BookingFunnelConversionMetric]
    overall_conversion: BookingFunnelOverallConversion | None
    drop_offs: list[BookingFunnelDropOffMetric]
    operational_alerts: list[BookingFunnelOperationalAlert]
    alert_thresholds: BookingFunnelAlertThresholds
    no_slot_dates: list[BookingFunnelNoSlotDateMetric]
    no_slot_contexts: list[BookingFunnelNoSlotContextMetric]
    no_slot_context_limit: int
    no_slot_contexts_truncated: bool
    no_slot_unknown_date_count: int
    unattributed_booking_successes: int
    weekly_insight_uk: str
    recommended_action: BookingFunnelRecommendedAction | None
    latest_weekly_digest: BookingFunnelWeeklyDigestResponse | None
