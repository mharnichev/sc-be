from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
PositiveId = Annotated[int, Field(strict=True, ge=1, le=2147483647)]


class PublicBookingFunnelObservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: Identifier
    anonymous_session_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
    event_type: Literal["time_step_viewed", "availability_requested", "availability_result", "availability_failed", "availability_cancelled"]
    master_id: PositiveId
    service_ids: list[PositiveId] = Field(min_length=1, max_length=10)
    target_date: date
    duration_minutes: int = Field(strict=True, ge=1, le=720)
    trigger: Literal["initial", "date_change", "refresh"]
    availability_request_id: Identifier | None = None
    slot_count: int | None = Field(default=None, strict=True, ge=0, le=5000)
    error_code: Literal["timeout", "network", "http_4xx", "http_5xx", "invalid_response"] | None = None
    cancel_reason: Literal["superseded", "navigation"] | None = None

    @field_validator("service_ids")
    @classmethod
    def normalize_services(cls, values):
        return sorted(set(values))

    @model_validator(mode="after")
    def event_fields(self):
        if (self.availability_request_id is None) != (self.event_type == "time_step_viewed"):
            raise ValueError("availability_request_id is required only for availability events")
        for field, kind in [("slot_count", "availability_result"), ("error_code", "availability_failed"), ("cancel_reason", "availability_cancelled")]:
            if (getattr(self, field) is not None) != (self.event_type == kind):
                raise ValueError(f"{field} is required only for {kind}")
        return self
