from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator


class FeedbackEmailRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    text: str = Field(min_length=3, max_length=5000)

    @field_validator("name", "text")
    @classmethod
    def reject_script_markup(cls, value: str) -> str:
        lowered = value.lower()
        if "<script" in lowered or "</script" in lowered:
            raise ValueError("Script markup is not allowed")
        return value.strip()


class FeedbackEmailResponse(BaseModel):
    message: str
