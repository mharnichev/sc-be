from __future__ import annotations

from datetime import date, datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class BookingAlternativesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    master_id: int = Field(gt=0)
    service_ids: list[int] = Field(min_length=1, max_length=20)
    desired_date: date
    duration_minutes: int = Field(gt=0, le=720)
    another_master_acceptable: bool = Field(
        default=True,
        validation_alias=AliasChoices("another_master_acceptable", "anotherMasterAcceptable"),
    )
    funnel_session_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        validation_alias=AliasChoices("funnel_session_id", "funnelSessionId"),
    )

    @field_validator("service_ids")
    @classmethod
    def service_ids_are_unique(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("service_ids must contain positive IDs")
        if len(set(value)) != len(value):
            raise ValueError("service_ids must not contain duplicates")
        return value


class AlternativeMasterPublic(BaseModel):
    id: int
    name: str
    photo_url: str | None = None
    avatar_url: str | None = None
    role: str | None = None
    rating_summary: float | None = None


class BookingAlternativeSlot(BaseModel):
    master: AlternativeMasterPublic
    start_at: datetime
    end_at: datetime
    date: date
    duration_minutes: int


class BookingAlternativesResponse(BaseModel):
    same_master: list[BookingAlternativeSlot] = Field(default_factory=list)
    other_masters: list[BookingAlternativeSlot] = Field(default_factory=list)
