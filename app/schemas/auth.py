from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel, TimestampedResponse


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminUserResponse(TimestampedResponse):
    id: int
    email: EmailStr
    is_active: bool
    is_superuser: bool


class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str
    is_active: bool = True
    is_superuser: bool = False


class AdminUserUpdate(BaseModel):
    password: str | None = None
    is_active: bool | None = None
    is_superuser: bool | None = None


class CustomerOtpRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=32)


class CustomerOtpRequestResponse(BaseModel):
    message: str
    expires_in_seconds: int
    retry_after_seconds: int
    sends_left_today: int
    debug_otp_code: str | None = None


class CustomerOtpVerifyRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    otp_code: str = Field(min_length=4, max_length=8)


class CustomerResponse(TimestampedResponse):
    id: int
    phone: str
    email: EmailStr | None
    name: str | None
    surname: str | None
    birthday: date | None
    is_active: bool
    phone_verified_at: datetime | None
    last_login_at: datetime | None


class CustomerSummaryResponse(ORMModel):
    id: int
    phone: str
    name: str | None
    surname: str | None
    is_verified: bool


class CustomerUpdate(BaseModel):
    phone: str | None = Field(default=None, min_length=7, max_length=32)
    email: EmailStr | None = None
    name: str | None = Field(default=None, max_length=100)
    surname: str | None = Field(default=None, max_length=100)
    birthday: date | None = None
    is_active: bool | None = None


class CustomerCreate(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    email: EmailStr | None = None
    name: str | None = Field(default=None, max_length=100)
    surname: str | None = Field(default=None, max_length=100)
    birthday: date | None = None
    is_active: bool = True


class CustomerAuthResponse(TokenResponse):
    customer: CustomerResponse
    is_new_customer: bool
    attempts_left_today: int
