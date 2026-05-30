from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.booking import BookingStatus, MasterPosition
from app.schemas.common import ORMModel, TimestampedResponse
from app.schemas.upload import UploadResponse


def sync_service_text_data(data: dict[str, Any]) -> dict[str, Any]:
    has_title_uk = "title_uk" in data
    has_name = "name" in data
    if has_title_uk and data["title_uk"] is not None and (not has_name or data["name"] is None):
        data["name"] = data["title_uk"]
    elif has_name and data["name"] is not None and (not has_title_uk or data["title_uk"] is None):
        data["title_uk"] = data["name"]

    has_description_uk = "description_uk" in data
    has_description = "description" in data
    if has_description_uk and (not has_description or data["description"] is None):
        data["description"] = data["description_uk"]
    elif has_description and (not has_description_uk or data["description_uk"] is None):
        data["description_uk"] = data["description"]
    return data


class ServiceTextFields(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    title_uk: str | None = Field(default=None, min_length=2, max_length=255)
    title_en: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    description_uk: str | None = None
    description_en: str | None = None

    @model_validator(mode="after")
    def sync_legacy_text_fields(self) -> "ServiceTextFields":
        if self.name is None and self.title_uk is not None:
            self.name = self.title_uk
        elif self.title_uk is None and self.name is not None:
            self.title_uk = self.name

        if self.description is None and self.description_uk is not None:
            self.description = self.description_uk
        elif self.description_uk is None and self.description is not None:
            self.description_uk = self.description
        return self


class ServiceFields(ServiceTextFields):
    duration_minutes: int = Field(gt=0, le=720)
    price: int = Field(ge=0)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_title_present(self) -> "ServiceFields":
        if self.name is None:
            raise ValueError("name or title_uk is required")
        return self


class BaseServiceCreate(ServiceFields):
    pass


class BaseServiceUpdate(ServiceTextFields):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    price: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class BaseServiceResponse(TimestampedResponse):
    id: int
    name: str
    title_uk: str | None = None
    title_en: str | None = None
    description: str | None
    description_uk: str | None = None
    description_en: str | None = None
    duration_minutes: int
    price: int
    is_active: bool


class BarberServiceCreate(ServiceTextFields):
    base_service_id: int | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    price: int | None = Field(default=None, ge=0)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_custom_service_fields(self) -> "BarberServiceCreate":
        if self.base_service_id is None:
            missing = [
                field
                for field in ("name", "duration_minutes", "price")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError("name/title_uk, duration_minutes and price are required for custom barber services")
        return self


class BarberServiceUpdate(ServiceTextFields):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    price: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    base_service_id: int | None = None


class BarberServiceBaseServiceResponse(ORMModel):
    id: int
    name: str
    title_uk: str | None = None
    title_en: str | None = None
    description: str | None = None
    description_uk: str | None = None
    description_en: str | None = None
    duration_minutes: int
    price: int
    is_active: bool


class BarberServiceResponse(TimestampedResponse):
    id: int
    barber_id: int
    base_service_id: int | None
    source_type: str
    name: str
    title_uk: str | None = None
    title_en: str | None = None
    description: str | None
    description_uk: str | None = None
    description_en: str | None = None
    duration_minutes: int
    price: int
    is_active: bool
    base_service: BarberServiceBaseServiceResponse | None = None


class PublicServiceCatalogBarberService(ORMModel):
    id: int
    barber_id: int
    name: str
    title_uk: str | None = None
    title_en: str | None = None
    description: str | None
    description_uk: str | None = None
    description_en: str | None = None
    duration_minutes: int
    price: int
    is_active: bool


class PublicServiceCatalogItem(BaseModel):
    catalog_id: str
    base_service_id: int | None
    source_type: str
    name: str
    title_uk: str | None = None
    title_en: str | None = None
    description: str | None
    description_uk: str | None = None
    description_en: str | None = None
    duration_minutes: int
    price: int
    barber_ids: list[int]
    barber_service_ids: list[int]
    barber_services: list[PublicServiceCatalogBarberService]


class SyncDefaultServicesResponse(BaseModel):
    barber_id: int
    created_count: int


BookingServiceCreate = BarberServiceCreate
BookingServiceUpdate = BarberServiceUpdate
BookingServiceResponse = BarberServiceResponse


class MasterBase(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    last_name: str | None = Field(default=None, min_length=2, max_length=255)
    first_name_en: str | None = Field(default=None, min_length=2, max_length=255)
    last_name_en: str | None = Field(default=None, min_length=2, max_length=255)
    position: MasterPosition = MasterPosition.master
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    description: str | None = None
    photo_url: str | None = Field(default=None, max_length=500)
    photo_upload_id: int | None = None
    avatar_url: str | None = Field(default=None, max_length=500)
    avatar_upload_id: int | None = None
    is_active: bool = True
    service_ids: list[int] = Field(default_factory=list)
    admin_user_id: int | None = None


class MasterCreate(MasterBase):
    password: str | None = Field(default=None, min_length=6, max_length=128)


class MasterUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    last_name: str | None = Field(default=None, min_length=2, max_length=255)
    first_name_en: str | None = Field(default=None, min_length=2, max_length=255)
    last_name_en: str | None = Field(default=None, min_length=2, max_length=255)
    position: MasterPosition | None = None
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    description: str | None = None
    photo_url: str | None = Field(default=None, max_length=500)
    photo_upload_id: int | None = None
    avatar_url: str | None = Field(default=None, max_length=500)
    avatar_upload_id: int | None = None
    is_active: bool | None = None
    service_ids: list[int] | None = None
    admin_user_id: int | None = None


class MasterResponse(TimestampedResponse):
    id: int
    admin_user_id: int | None
    full_name: str
    last_name: str | None = None
    first_name_uk: str
    last_name_uk: str | None = None
    first_name_en: str | None = None
    last_name_en: str | None = None
    full_name_uk: str
    full_name_en: str | None = None
    position: MasterPosition
    position_uk: str
    position_en: str
    email: str | None
    phone: str | None
    description: str | None
    photo_url: str | None
    photo_upload_id: int | None
    photo: UploadResponse | None = None
    avatar_url: str | None
    avatar_upload_id: int | None
    avatar: UploadResponse | None = None
    is_active: bool
    services: list[BarberServiceResponse] = Field(default_factory=list)

    @field_validator("position", mode="before")
    @classmethod
    def default_position(cls, value: MasterPosition | str | None) -> MasterPosition:
        return value or MasterPosition.master


class AvailableSlotResponse(BaseModel):
    start_at: datetime
    end_at: datetime


def normalize_service_ids(service_id: int | None, service_ids: list[int] | None) -> tuple[int, list[int]]:
    ids = list(service_ids or ([] if service_id is None else [service_id]))
    if service_id is not None and service_id not in ids:
        ids.insert(0, service_id)
    if not ids:
        raise ValueError("service_id or service_ids is required")
    if len(set(ids)) != len(ids):
        raise ValueError("service_ids must not contain duplicates")
    return ids[0], ids


class PublicBookingCreate(BaseModel):
    master_id: int
    service_id: int | None = None
    service_ids: list[int] | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    customer_name: str = Field(min_length=2, max_length=255)
    customer_phone: str = Field(min_length=5, max_length=50)
    customer_email: EmailStr | None = None
    customer_comment: str | None = None
    start_at: datetime

    @model_validator(mode="after")
    def validate_services(self) -> "PublicBookingCreate":
        self.service_id, self.service_ids = normalize_service_ids(self.service_id, self.service_ids)
        return self


class BookingCustomerResponse(ORMModel):
    id: int
    phone: str
    email: EmailStr | None = None
    name: str | None = None
    surname: str | None = None


class BookingResponse(TimestampedResponse):
    id: int
    master_id: int
    service_id: int
    service_ids: list[int]
    services: list[BarberServiceResponse] = Field(default_factory=list)
    customer_id: int | None = None
    customer_name: str
    customer_phone: str
    customer_email: EmailStr | None = None
    customer_comment: str | None
    start_at: datetime
    end_at: datetime
    status: BookingStatus
    cancelled_at: datetime | None
    completed_at: datetime | None
    customer: BookingCustomerResponse | None = None


class BookingUpdate(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    service_ids: list[int] | None = None

    @model_validator(mode="after")
    def validate_service_ids(self) -> "BookingUpdate":
        if self.service_ids is not None:
            if not self.service_ids:
                raise ValueError("service_ids must contain at least one service")
            if len(set(self.service_ids)) != len(self.service_ids):
                raise ValueError("service_ids must not contain duplicates")
        return self


class CustomerBookingStatsItem(BaseModel):
    id: int | None = None
    name: str
    count: int


class CustomerBookingStatsResponse(BaseModel):
    total_bookings: int
    most_visited_barber: CustomerBookingStatsItem | None
    most_used_services: list[CustomerBookingStatsItem]
    last_visit_date: datetime | None


class BookingStatusUpdate(BaseModel):
    status: BookingStatus

    @field_validator("status")
    @classmethod
    def status_must_be_actionable(cls, value: BookingStatus) -> BookingStatus:
        if value == BookingStatus.pending:
            raise ValueError("Pending booking status is not supported")
        return value


class MasterTimeBlockCreate(BaseModel):
    start_at: datetime
    end_at: datetime
    reason: str | None = None


class AdminMasterTimeBlockCreate(MasterTimeBlockCreate):
    master_id: int


class MasterTimeBlockResponse(TimestampedResponse):
    id: int
    master_id: int
    start_at: datetime
    end_at: datetime
    reason: str | None
