from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.utils.shop_input import email_input, message_input, person_name, phone_input

from app.schemas.common import ORMModel, TimestampedResponse


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BackofficeTokenResponse(TokenResponse):
    refresh_token: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class AdminUserResponse(TimestampedResponse):
    id: int
    email: EmailStr
    is_active: bool
    is_superuser: bool
    can_post_inventory: bool = False
    can_accept_qr_payments: bool = False
    can_record_sales: bool = False
    can_manage_shipments: bool = False
    can_fiscalize_returns: bool = False
    can_fiscalize_sales: bool = False
    can_manage_fiscal_settings: bool = False
    role: Literal["admin", "barber"] | None = None
    master_id: int | None = None


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
    phone: str = Field(min_length=10, max_length=20)

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value):
        return phone_input(value)


class CustomerOtpRequestResponse(BaseModel):
    message: str
    expires_in_seconds: int
    retry_after_seconds: int
    sends_left_today: int
    debug_otp_code: str | None = None


class CustomerOtpVerifyRequest(CustomerOtpRequest):
    otp_code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class CustomerPhoneChangeConfirm(CustomerOtpVerifyRequest):
    pass


class CustomerResponse(TimestampedResponse):
    id: int
    phone: str
    email: EmailStr | None
    name: str | None
    surname: str | None
    birthday: date | None
    notes: str | None = None
    imported_total_spent: Decimal = Decimal("0.00")
    imported_last_visit_at: datetime | None = None
    imported_is_new_client: bool = False
    is_active: bool
    phone_verified_at: datetime | None
    last_login_at: datetime | None


class CustomerSummaryResponse(ORMModel):
    id: int
    phone: str
    email: EmailStr | None = None
    name: str | None
    surname: str | None
    notes: str | None = None
    imported_total_spent: Decimal = Decimal("0.00")
    imported_last_visit_at: datetime | None = None
    imported_is_new_client: bool = False
    is_verified: bool
    telegram_connected: bool = False


class CustomerProfileUpdate(BaseModel):
    phone: str | None = Field(default=None, min_length=7, max_length=32)
    email: EmailStr | None = None
    name: str | None = Field(default=None, max_length=100)
    surname: str | None = Field(default=None, max_length=100)
    birthday: date | None = None
    notes: str | None = None


class PublicCustomerProfileUpdate(CustomerProfileUpdate):
    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value):
        return phone_input(value)

    @field_validator("name", "surname", mode="before")
    @classmethod
    def normalize_name(cls, value):
        return person_name(value, minimum=0)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return email_input(value)

    @field_validator("notes", mode="before")
    @classmethod
    def normalize_notes(cls, value):
        return message_input(value, 500)

    @field_validator("birthday", mode="before")
    @classmethod
    def validate_birthday_input(cls, value):
        if value is not None and type(value) is not date and not (
            isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
        ):
            raise ValueError("Birthday must be a YYYY-MM-DD date")
        return value

    @field_validator("birthday")
    @classmethod
    def validate_birthday(cls, value):
        if value is not None and not date(1900, 1, 1) <= value <= date.today():
            raise ValueError("Birthday must be between 1900 and today")
        return value


class CustomerUpdate(CustomerProfileUpdate):
    imported_total_spent: Decimal | None = Field(default=None, ge=0)
    imported_last_visit_at: datetime | None = None
    imported_is_new_client: bool | None = None
    is_active: bool | None = None


class CustomerCreate(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    email: EmailStr | None = None
    name: str | None = Field(default=None, max_length=100)
    surname: str | None = Field(default=None, max_length=100)
    birthday: date | None = None
    notes: str | None = None
    imported_total_spent: Decimal = Field(default=Decimal("0.00"), ge=0)
    imported_last_visit_at: datetime | None = None
    imported_is_new_client: bool = False
    is_active: bool = True


class CustomerAuthResponse(TokenResponse):
    customer: CustomerResponse
    is_new_customer: bool
    attempts_left_today: int
