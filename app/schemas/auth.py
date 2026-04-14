from __future__ import annotations

from pydantic import BaseModel, EmailStr

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
