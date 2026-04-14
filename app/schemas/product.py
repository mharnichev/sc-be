from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.brand import BrandResponse
from app.schemas.category import CategoryResponse
from app.schemas.common import TimestampedResponse


class ProductBase(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    slug: str = Field(min_length=2, max_length=255)
    description: str | None = None
    short_description: str | None = Field(default=None, max_length=500)
    price: Decimal = Field(gt=0)
    old_price: Decimal | None = Field(default=None, gt=0)
    sku: str | None = Field(default=None, max_length=100)
    stock_quantity: int = Field(default=0, ge=0)
    is_active: bool = True
    brand_id: int | None = None
    category_id: int | None = None


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    slug: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    short_description: str | None = Field(default=None, max_length=500)
    price: Decimal | None = Field(default=None, gt=0)
    old_price: Decimal | None = Field(default=None, gt=0)
    sku: str | None = Field(default=None, max_length=100)
    stock_quantity: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    brand_id: int | None = None
    category_id: int | None = None


class ProductResponse(TimestampedResponse):
    id: int
    name: str
    slug: str
    description: str | None
    short_description: str | None
    price: Decimal
    old_price: Decimal | None
    sku: str | None
    stock_quantity: int
    is_active: bool
    brand_id: int | None
    category_id: int | None
    brand: BrandResponse | None = None
    category: CategoryResponse | None = None
