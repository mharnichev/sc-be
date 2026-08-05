from __future__ import annotations

from datetime import date, datetime, time

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.waitlist import WaitlistStatus


class PublicWaitlistCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_name: str = Field(min_length=2, max_length=255)
    customer_phone: str = Field(min_length=5, max_length=50)
    service_ids: list[int] = Field(min_length=1, max_length=10)
    preferred_master_id: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices("preferred_master_id", "preferredMasterId"),
    )
    desired_date: date
    acceptable_date_from: date | None = None
    acceptable_date_to: date | None = None
    preferred_time_from: time | None = None
    preferred_time_to: time | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    notification_consent: bool

    @field_validator("customer_name")
    @classmethod
    def normalize_customer_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("customer_name is required")
        return normalized

    @field_validator("service_ids")
    @classmethod
    def service_ids_must_be_positive(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("service_ids must contain positive IDs")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> "PublicWaitlistCreate":
        if len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError("service_ids must not contain duplicates")
        if self.acceptable_date_from and self.acceptable_date_to and self.acceptable_date_from > self.acceptable_date_to:
            raise ValueError("acceptable_date_from must not be after acceptable_date_to")
        if self.acceptable_date_from and self.acceptable_date_from > self.desired_date:
            raise ValueError("acceptable_date_from must include desired_date")
        if self.acceptable_date_to and self.acceptable_date_to < self.desired_date:
            raise ValueError("acceptable_date_to must include desired_date")
        if self.preferred_time_from and self.preferred_time_to and self.preferred_time_from >= self.preferred_time_to:
            raise ValueError("preferred_time_from must be before preferred_time_to")
        return self


class PublicWaitlistResponse(BaseModel):
    public_id: str
    status: WaitlistStatus
    expires_at: datetime
    cancel_token: str


class PublicWaitlistCancel(BaseModel):
    cancel_token: str = Field(min_length=20, max_length=512)


class PublicWaitlistCancelResponse(BaseModel):
    public_id: str
    status: WaitlistStatus


class PublicWaitlistOfferClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")
