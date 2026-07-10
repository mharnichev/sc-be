from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import TimestampedResponse
from app.schemas.product import ShopProductResponse


class CartItemCreate(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(default=1, ge=1)


class CartItemResponse(TimestampedResponse):
    id: int
    product_id: int
    quantity: int
    product: ShopProductResponse


class WishlistItemCreate(BaseModel):
    product_id: int = Field(gt=0)


class WishlistItemResponse(TimestampedResponse):
    id: int
    product_id: int
    product: ShopProductResponse
