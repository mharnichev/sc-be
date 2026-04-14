from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class UploadCreate(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    file_path: str = Field(min_length=1, max_length=500)
    file_url: str | None = None
    content_type: str | None = None
    size: int | None = Field(default=None, ge=0)


class UploadResponse(ORMModel):
    id: int
    file_name: str
    file_path: str
    file_url: str | None
    content_type: str | None
    size: int | None
    created_at: datetime
