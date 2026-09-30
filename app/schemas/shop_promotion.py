from __future__ import annotations

import re

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.shop_promotion import (
    ShopPromotionDiscountType,
    ShopPromotionStatus,
    ShopPromotionTrigger,
    normalize_shop_promotion_datetime,
    shop_promotion_status,
)
from app.schemas.common import TimestampedResponse

SHOP_PROMOTION_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]+$")


def normalize_shop_promotion_code(value: str) -> str:
    return value.strip().upper()


class ShopPromotionBase(BaseModel):
    """Product discount contract; naive period values are interpreted in Europe/Kyiv."""

    name: str = Field(min_length=2, max_length=255)
    description: str | None = None
    trigger: ShopPromotionTrigger
    code: str | None = Field(default=None, min_length=3, max_length=50)
    discount_type: ShopPromotionDiscountType
    discount_value: Decimal = Field(gt=0)
    priority: int = Field(default=100, ge=0, le=10000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    usage_limit: int | None = Field(
        default=None,
        gt=0,
        description="Supported for promocode promotions only; automatic discounts reject usage limits.",
    )
    usage_limit_per_customer: int | None = Field(
        default=None,
        gt=0,
        description="Supported for promocode promotions only; automatic discounts reject usage limits.",
    )
    applies_to_all_products: bool = False
    include_subcategories: bool = True
    product_ids: list[int] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)
    brand_ids: list[int] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("code must be a string")
        normalized = normalize_shop_promotion_code(value)
        if not SHOP_PROMOTION_CODE_PATTERN.match(normalized):
            raise ValueError("code may contain only A-Z, 0-9, '_' and '-'")
        return normalized

    @field_validator("starts_at", "ends_at", mode="after")
    @classmethod
    def normalize_period_datetime(cls, value: datetime | None) -> datetime | None:
        return normalize_shop_promotion_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def validate_rule(self) -> "ShopPromotionBase":
        if self.trigger == ShopPromotionTrigger.promocode and not self.code:
            raise ValueError("code is required for promocode promotions")
        if self.trigger == ShopPromotionTrigger.automatic:
            if self.code:
                raise ValueError("code is not allowed for automatic promotions")
            if self.usage_limit is not None or self.usage_limit_per_customer is not None:
                raise ValueError("usage limits are supported for promocode promotions only")
        if self.discount_type == ShopPromotionDiscountType.percent and self.discount_value > 100:
            raise ValueError("percent discount_value must be at most 100")
        if self.starts_at is not None and self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        for field_name in ("product_ids", "category_ids", "brand_ids"):
            values = getattr(self, field_name)
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must not contain duplicates")
        if self.applies_to_all_products:
            self.product_ids = []
            self.category_ids = []
            self.brand_ids = []
        elif not (self.product_ids or self.category_ids or self.brand_ids):
            raise ValueError("at least one product, category or brand is required")
        return self


class ShopPromotionCreate(ShopPromotionBase):
    pass


class ShopPromotionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    trigger: ShopPromotionTrigger | None = None
    code: str | None = Field(default=None, min_length=3, max_length=50)
    discount_type: ShopPromotionDiscountType | None = None
    discount_value: Decimal | None = Field(default=None, gt=0)
    priority: int | None = Field(default=None, ge=0, le=10000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    usage_limit: int | None = Field(default=None, gt=0)
    usage_limit_per_customer: int | None = Field(default=None, gt=0)
    applies_to_all_products: bool | None = None
    include_subcategories: bool | None = None
    product_ids: list[int] | None = None
    category_ids: list[int] | None = None
    brand_ids: list[int] | None = None
    is_active: bool | None = None

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("code must be a string")
        normalized = normalize_shop_promotion_code(value)
        if not SHOP_PROMOTION_CODE_PATTERN.match(normalized):
            raise ValueError("code may contain only A-Z, 0-9, '_' and '-'")
        return normalized

    @field_validator("starts_at", "ends_at", mode="after")
    @classmethod
    def normalize_period_datetime(cls, value: datetime | None) -> datetime | None:
        return normalize_shop_promotion_datetime(value) if value is not None else None

    @field_validator("product_ids", "category_ids", "brand_ids")
    @classmethod
    def reject_duplicate_scope_ids(cls, values: list[int] | None) -> list[int] | None:
        if values is not None and len(values) != len(set(values)):
            raise ValueError("applicability ids must not contain duplicates")
        return values


class ShopPromotionResponse(TimestampedResponse):
    id: int
    name: str
    description: str | None = None
    trigger: ShopPromotionTrigger
    code: str | None = None
    discount_type: ShopPromotionDiscountType
    discount_value: Decimal
    priority: int
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    usage_limit: int | None = None
    usage_limit_per_customer: int | None = None
    applies_to_all_products: bool
    include_subcategories: bool
    product_ids: list[int] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)
    brand_ids: list[int] = Field(default_factory=list)
    is_active: bool

    @field_validator("starts_at", "ends_at", mode="after")
    @classmethod
    def normalize_response_period_datetime(cls, value: datetime | None) -> datetime | None:
        return normalize_shop_promotion_datetime(value) if value is not None else None

    @computed_field
    @property
    def status(self) -> ShopPromotionStatus:
        return shop_promotion_status(
            is_active=self.is_active,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
        )


class ShopPromotionProductResponse(BaseModel):
    product_id: int
    product_name: str
    sku: str | None = None
    base_price: Decimal
    is_effectively_visible: bool
    hidden_reason: str | None = None


class ShopPromotionProductsResponse(BaseModel):
    affected_products_count: int
    products: list[ShopPromotionProductResponse] = Field(default_factory=list)


class ShopPromotionPreviewPromotion(BaseModel):
    id: int | None
    name: str
    code: str | None = None
    trigger: ShopPromotionTrigger
    price: Decimal


class ShopPromotionPreviewConflict(BaseModel):
    promotion_id: int
    name: str
    trigger: ShopPromotionTrigger
    priority: int
    price: Decimal


class ShopPromotionPreviewItem(BaseModel):
    product_id: int
    product_name: str
    base_price: Decimal
    new_price: Decimal
    discount_amount: Decimal
    currently_applied_promotion: ShopPromotionPreviewPromotion | None = None
    applied_promotion: ShopPromotionPreviewPromotion | None = None
    conflicts: list[ShopPromotionPreviewConflict] = Field(default_factory=list)


class ShopPromotionPreviewResponse(BaseModel):
    evaluated_at: datetime
    affected_products_count: int
    products: list[ShopPromotionPreviewItem] = Field(default_factory=list)


class ShopPromotionQuoteItem(BaseModel):
    product_id: int
    quantity: int
    base_price: Decimal
    price: Decimal
    discount_amount: Decimal
    promotion_id: int | None = None
    promotion_name: str | None = None
    promotion_code: str | None = None


class ShopPromotionQuoteRequestItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    product_id: int = Field(alias="productId")
    quantity: int = Field(ge=1, le=100)


class ShopPromotionQuoteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: list[ShopPromotionQuoteRequestItem] = Field(min_length=1)
    promo_code: str | None = Field(default=None, alias="promoCode", min_length=3, max_length=50)
    customer_phone: str | None = Field(default=None, alias="customerPhone", min_length=5, max_length=50)

    @field_validator("promo_code", mode="before")
    @classmethod
    def normalize_code(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_shop_promotion_code(value)


class ShopPromotionQuoteResponse(BaseModel):
    subtotal_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    requested_code: str | None = None
    applied_code: str | None = None
    items: list[ShopPromotionQuoteItem] = Field(default_factory=list)
