from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import TimestampedResponse


class CategoryBase(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    slug: str = Field(min_length=2, max_length=255)
    description: str | None = None
    is_active: bool = True
    parent_id: int | None = None


class CategoryCreate(CategoryBase):
    pass


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    slug: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    parent_id: int | None = None


class CategoryResponse(TimestampedResponse):
    id: int
    name: str
    slug: str
    description: str | None
    is_active: bool
    parent_id: int | None


class BackofficeCategoryResponse(CategoryResponse):
    is_effectively_visible: bool
    hidden_reason: Literal["category", "parent_category"] | None


class CategoryTreeNode(CategoryResponse):
    children: list["CategoryTreeNode"] = Field(default_factory=list)


class BackofficeCategoryTreeNode(BackofficeCategoryResponse):
    children: list["BackofficeCategoryTreeNode"] = Field(default_factory=list)
