from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.utils.shop_input import email_input, message_input, person_name


class FeedbackEmailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=2, max_length=80)
    email: EmailStr = Field(max_length=254)
    text: str = Field(min_length=10, max_length=2000)
    topic: Literal["general", "order", "delivery", "returns", "product", "other"] = "general"
    order_id: int | None = Field(default=None, alias="orderReference", gt=0)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        return person_name(value, 2)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return email_input(value)

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value):
        return message_input(value, 2000, 10)


class FeedbackEmailResponse(BaseModel):
    message: str
