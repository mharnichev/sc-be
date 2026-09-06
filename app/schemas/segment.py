from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.segment import SegmentStatus


class StrictRule(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SegmentPeriod(StrictRule):
    """Absolute [start, end), or trailing [at - last units, at)."""

    start: datetime | None = None
    end: datetime | None = None
    last: int | None = Field(default=None, ge=1, le=3660)
    unit: Literal["days", "calendar_months"] = "days"

    @model_validator(mode="after")
    def validate_period(self) -> "SegmentPeriod":
        if self.last is not None:
            if self.start is not None or self.end is not None:
                raise ValueError("Use either last/unit or start/end")
            if self.unit == "calendar_months" and self.last > 120:
                raise ValueError("Calendar month periods are limited to 120 months")
        elif self.start is None or self.end is None:
            raise ValueError("Absolute periods require start and end")
        else:
            if self.start.utcoffset() is None or self.end.utcoffset() is None:
                raise ValueError("Period timestamps must include a timezone offset")
            if self.start >= self.end:
                raise ValueError("Period start must precede end")
        return self


class LastVisitAgeCondition(StrictRule):
    type: Literal["last_visit_age"]
    min: int | None = Field(default=None, ge=0, le=36600)
    max: int | None = Field(default=None, ge=0, le=36600)
    unit: Literal["days", "calendar_months"] = "calendar_months"
    min_inclusive: bool = False
    max_inclusive: bool = True

    @model_validator(mode="after")
    def validate_bounds(self) -> "LastVisitAgeCondition":
        if self.min is None and self.max is None:
            raise ValueError("At least one age bound is required")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        if self.unit == "calendar_months" and max(self.min or 0, self.max or 0) > 1200:
            raise ValueError("Calendar month age is limited to 1200 months")
        return self


class CompletedVisitCountCondition(StrictRule):
    type: Literal["completed_visit_count"]
    min: int | None = Field(default=None, ge=0, le=1000000)
    max: int | None = Field(default=None, ge=0, le=1000000)
    period: SegmentPeriod | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "CompletedVisitCountCondition":
        if self.min is None and self.max is None:
            raise ValueError("At least one count bound is required")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        return self


class UpcomingBookingCondition(StrictRule):
    type: Literal["upcoming_booking"]
    present: bool = True


class VisitedMasterCondition(StrictRule):
    type: Literal["visited_master"]
    master_ids: list[int] = Field(min_length=1, max_length=50)
    mode: Literal["last", "within_period"] = "last"
    period: SegmentPeriod | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> "VisitedMasterCondition":
        if any(value <= 0 for value in self.master_ids):
            raise ValueError("master_ids must be positive")
        if (self.mode == "within_period") != (self.period is not None):
            raise ValueError("period is required only for within_period mode")
        return self


class ReceivedServiceCondition(StrictRule):
    type: Literal["received_service"]
    # IDs from the existing barber_services catalog, including custom services.
    service_ids: list[int] = Field(min_length=1, max_length=50)
    period: SegmentPeriod

    @field_validator("service_ids")
    @classmethod
    def positive_ids(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("service_ids must be positive")
        return values


class FirstVisitCondition(StrictRule):
    type: Literal["first_visit"]
    period: SegmentPeriod


class ReceivedCampaignCondition(StrictRule):
    type: Literal["received_campaign"]
    campaign_id: int = Field(gt=0)
    period: SegmentPeriod | None = None


class MarketingContactCondition(StrictRule):
    type: Literal["marketing_contact"]
    period: SegmentPeriod
    present: bool = True


SegmentCondition = Annotated[
    LastVisitAgeCondition | CompletedVisitCountCondition | UpcomingBookingCondition
    | VisitedMasterCondition | ReceivedServiceCondition | FirstVisitCondition
    | ReceivedCampaignCondition | MarketingContactCondition,
    Field(discriminator="type"),
]


class SegmentRules(StrictRule):
    combine: Literal["all", "any"] = "all"
    conditions: list[SegmentCondition] = Field(min_length=1, max_length=20)
    exclusions: list[SegmentCondition] = Field(default_factory=list, max_length=20)


class SegmentCreate(StrictRule):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    rules: SegmentRules

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class SegmentUpdate(StrictRule):
    expected_revision: int = Field(ge=1, description="Expected current revision; stale edits return 409")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    rules: SegmentRules | None = None

    @model_validator(mode="after")
    def validate_update(self) -> "SegmentUpdate":
        if "name" in self.model_fields_set:
            if self.name is None or not self.name.strip():
                raise ValueError("name must not be blank")
            self.name = self.name.strip()
        if "rules" in self.model_fields_set and self.rules is None:
            raise ValueError("rules cannot be null")
        return self


class SegmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None
    status: SegmentStatus
    rules: SegmentRules
    revision: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class SegmentList(BaseModel):
    items: list[SegmentRead]
    total: int
    limit: int
    offset: int


SegmentResponse = SegmentRead


class SegmentPreviewRequest(StrictRule):
    rules: SegmentRules
    evaluated_at: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1000000)

    @field_validator("evaluated_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("evaluated_at must include a timezone offset")
        return value


class SegmentMember(BaseModel):
    customer_id: int
    name: str | None
    phone: str
    history_state: Literal["known", "no_visits", "unknown"]
    last_visit_at: datetime | None
    completed_visit_count: int
    first_completed_visit_at: datetime | None
    has_upcoming_booking: bool
    conditions: list[dict[str, Any]]
    exclusions: list[dict[str, Any]]


class SegmentPreviewResponse(BaseModel):
    evaluated_at: datetime
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    total: int
    items: list[SegmentMember]
    limit: int
    offset: int
