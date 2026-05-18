from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field, model_validator

from app.models.booking import BookingStatus
from app.schemas.common import ORMModel, TimestampedResponse
from app.schemas.upload import UploadResponse


class ServiceFields(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: str | None = None
    duration_minutes: int = Field(gt=0, le=720)
    price: int = Field(ge=0)
    is_active: bool = True


class BaseServiceCreate(ServiceFields):
    pass


class BaseServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    price: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class BaseServiceResponse(TimestampedResponse):
    id: int
    name: str
    description: str | None
    duration_minutes: int
    price: int
    is_active: bool


class BarberServiceCreate(BaseModel):
    base_service_id: int | None = None
    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
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
                raise ValueError("name, duration_minutes and price are required for custom barber services")
        return self


class BarberServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=720)
    price: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    base_service_id: int | None = None


class BarberServiceBaseServiceResponse(ORMModel):
    id: int
    name: str
    duration_minutes: int
    price: int
    is_active: bool


class BarberServiceResponse(TimestampedResponse):
    id: int
    barber_id: int
    base_service_id: int | None
    source_type: str
    name: str
    description: str | None
    duration_minutes: int
    price: int
    is_active: bool
    base_service: BarberServiceBaseServiceResponse | None = None


class PublicServiceCatalogBarberService(ORMModel):
    id: int
    barber_id: int
    name: str
    description: str | None
    duration_minutes: int
    price: int
    is_active: bool


class PublicServiceCatalogItem(BaseModel):
    catalog_id: str
    base_service_id: int | None
    source_type: str
    name: str
    description: str | None
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
    services: list[BarberServiceResponse] = []


class AvailableSlotResponse(BaseModel):
    start_at: datetime
    end_at: datetime


class PublicBookingCreate(BaseModel):
    master_id: int
    service_id: int
    customer_name: str = Field(min_length=2, max_length=255)
    customer_phone: str = Field(min_length=5, max_length=50)
    customer_comment: str | None = None
    start_at: datetime


class BookingResponse(TimestampedResponse):
    id: int
    master_id: int
    service_id: int
    customer_name: str
    customer_phone: str
    customer_comment: str | None
    start_at: datetime
    end_at: datetime
    status: BookingStatus
    cancelled_at: datetime | None


class BookingStatusUpdate(BaseModel):
    status: BookingStatus


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
