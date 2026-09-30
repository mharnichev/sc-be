from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class FeedbackEmailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    text: str = Field(min_length=3, max_length=5000)
    topic: Literal["general", "order", "delivery", "returns", "product", "other"] = "general"
    order_id: int | None = Field(default=None, alias="orderReference", gt=0)

    @field_validator("name", "text")
    @classmethod
    def reject_script_markup(cls, value: str) -> str:
        lowered = value.lower()
        if "<script" in lowered or "</script" in lowered:
            raise ValueError("Script markup is not allowed")
        return value.strip()


class FeedbackEmailResponse(BaseModel):
    message: str
