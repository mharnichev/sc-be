from __future__ import annotations

import re

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.promotion import PromotionDiscountType, PromotionEligibilityType
from app.schemas.common import TimestampedResponse

PROMOTION_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]+$")


def normalize_promotion_code(value: str) -> str:
    return value.strip().upper()


class PromotionBase(BaseModel):
    code: str = Field(min_length=3, max_length=50)
    name_uk: str = Field(min_length=2, max_length=255)
    name_en: str = Field(min_length=2, max_length=255)
    description_uk: str | None = None
    description_en: str | None = None
    discount_type: PromotionDiscountType = PromotionDiscountType.percent
    discount_percent: int = Field(gt=0, le=100)
    eligibility_type: PromotionEligibilityType = PromotionEligibilityType.all_customers
    inactive_days: int | None = Field(default=None, gt=0, le=3650)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    applies_to_all_masters: bool = True
    master_ids: list[int] = Field(default_factory=list)
    applies_to_all_services: bool = True
    base_service_ids: list[int] = Field(default_factory=list)
    is_public: bool = True
    is_active: bool = True

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("code must be a string")
        normalized = normalize_promotion_code(value)
        if not PROMOTION_CODE_PATTERN.match(normalized):
            raise ValueError("code may contain only A-Z, 0-9, '_' and '-'")
        return normalized

    @model_validator(mode="after")
    def validate_promotion_rules(self) -> "PromotionBase":
        if self.ends_at is not None and self.starts_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.eligibility_type == PromotionEligibilityType.inactive_customers and self.inactive_days is None:
            self.inactive_days = 90
        if self.eligibility_type != PromotionEligibilityType.inactive_customers:
            self.inactive_days = None
        if len(set(self.master_ids)) != len(self.master_ids):
            raise ValueError("master_ids must not contain duplicates")
        if len(set(self.base_service_ids)) != len(self.base_service_ids):
            raise ValueError("base_service_ids must not contain duplicates")
        if not self.applies_to_all_masters and not self.master_ids:
            raise ValueError("master_ids are required when applies_to_all_masters is false")
        if not self.applies_to_all_services and not self.base_service_ids:
            raise ValueError("base_service_ids are required when applies_to_all_services is false")
        if self.applies_to_all_masters:
            self.master_ids = []
        if self.applies_to_all_services:
            self.base_service_ids = []
        return self


class PromotionCreate(PromotionBase):
    pass


class PromotionUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=3, max_length=50)
    name_uk: str | None = Field(default=None, min_length=2, max_length=255)
    name_en: str | None = Field(default=None, min_length=2, max_length=255)
    description_uk: str | None = None
    description_en: str | None = None
    discount_type: PromotionDiscountType | None = None
    discount_percent: int | None = Field(default=None, gt=0, le=100)
    eligibility_type: PromotionEligibilityType | None = None
    inactive_days: int | None = Field(default=None, gt=0, le=3650)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    applies_to_all_masters: bool | None = None
    master_ids: list[int] | None = None
    applies_to_all_services: bool | None = None
    base_service_ids: list[int] | None = None
    is_public: bool | None = None
    is_active: bool | None = None

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("code must be a string")
        normalized = normalize_promotion_code(value)
        if not PROMOTION_CODE_PATTERN.match(normalized):
            raise ValueError("code may contain only A-Z, 0-9, '_' and '-'")
        return normalized

    @model_validator(mode="after")
    def validate_date_order(self) -> "PromotionUpdate":
        if self.ends_at is not None and self.starts_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.master_ids is not None and len(set(self.master_ids)) != len(self.master_ids):
            raise ValueError("master_ids must not contain duplicates")
        if self.base_service_ids is not None and len(set(self.base_service_ids)) != len(self.base_service_ids):
            raise ValueError("base_service_ids must not contain duplicates")
        return self


class PromotionResponse(TimestampedResponse):
    id: int
    code: str
    name_uk: str
    name_en: str
    description_uk: str | None = None
    description_en: str | None = None
    discount_type: PromotionDiscountType
    discount_percent: int
    eligibility_type: PromotionEligibilityType
    inactive_days: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    applies_to_all_masters: bool
    master_ids: list[int] = Field(default_factory=list)
    applies_to_all_services: bool
    base_service_ids: list[int] = Field(default_factory=list)
    is_public: bool
    is_active: bool
