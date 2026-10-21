from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.brand import BrandResponse
from app.schemas.category import CategoryResponse
from app.schemas.common import TimestampedResponse
from app.services.catalog_visibility import HiddenReason
from app.utils.product_html import sanitize_product_html
from app.utils.shop_input import message_input


class _SanitizedProductDescriptions(BaseModel):
    @field_validator("description", "short_description", "description_en", "short_description_en", check_fields=False)
    @classmethod
    def sanitize_descriptions(cls, value: str | None) -> str | None:
        return sanitize_product_html(value)


class ProductBase(_SanitizedProductDescriptions):
    name: str = Field(min_length=2, max_length=255)
    name_en: str | None = Field(default=None, min_length=2, max_length=255)
    old_name: str | None = Field(default=None, max_length=255)
    model_name: str | None = Field(default=None, max_length=255)
    product_type: str | None = Field(default=None, max_length=255)
    product_type_en: str | None = Field(default=None, max_length=255)
    package_size: str | None = Field(default=None, max_length=64)
    slug: str = Field(min_length=2, max_length=255)
    description: str | None = None
    description_en: str | None = None
    ingredients: str | None = Field(default=None, description="Склад: composition text, including any source qualifications.")
    ingredients_en: str | None = None
    short_description: str | None = None
    short_description_en: str | None = None
    price: Decimal = Field(gt=0)
    recommended_retail_price: Decimal | None = Field(default=None, gt=0)
    sku: str | None = Field(default=None, max_length=100)
    is_active: bool = True
    image_url: str | None = Field(default=None, max_length=500)
    external_url: str | None = Field(default=None, max_length=500)
    availability_status: str | None = Field(default=None, max_length=32, description="Supplier availability; independent of local inventory.")
    attributes_json: dict | None = None
    variant_group_key: str | None = Field(default=None, max_length=64)
    volume_ml: int | None = Field(default=None, gt=0)
    brand_id: int | None = None
    category_id: int | None = None


class ProductCreate(ProductBase):
    pass


class ProductUpdate(_SanitizedProductDescriptions):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    name_en: str | None = Field(default=None, min_length=2, max_length=255)
    old_name: str | None = Field(default=None, max_length=255)
    model_name: str | None = Field(default=None, max_length=255)
    product_type: str | None = Field(default=None, max_length=255)
    product_type_en: str | None = Field(default=None, max_length=255)
    package_size: str | None = Field(default=None, max_length=64)
    slug: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    description_en: str | None = None
    ingredients: str | None = None
    ingredients_en: str | None = None
    short_description: str | None = None
    short_description_en: str | None = None
    price: Decimal | None = Field(default=None, gt=0)
    recommended_retail_price: Decimal | None = Field(default=None, gt=0)
    sku: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
    image_url: str | None = Field(default=None, max_length=500)
    external_url: str | None = Field(default=None, max_length=500)
    availability_status: str | None = Field(default=None, max_length=32, description="Supplier availability; independent of local inventory.")
    attributes_json: dict | None = None
    variant_group_key: str | None = Field(default=None, max_length=64)
    volume_ml: int | None = Field(default=None, gt=0)
    brand_id: int | None = None
    category_id: int | None = None


class ProductResponse(TimestampedResponse, _SanitizedProductDescriptions):
    id: int
    name: str
    name_en: str | None = None
    old_name: str | None = None
    model_name: str | None = None
    product_type: str | None = None
    product_type_en: str | None = None
    package_size: str | None = None
    slug: str
    description: str | None
    description_en: str | None = None
    ingredients: str | None = None
    ingredients_en: str | None = None
    short_description: str | None
    short_description_en: str | None = None
    price: Decimal
    recommended_retail_price: Decimal | None
    sku: str | None
    stock_quantity: int
    reserved_quantity: int
    available_quantity: int
    barcode: str | None
    allow_backorder: bool
    is_active: bool
    image_url: str | None
    external_url: str | None
    availability_status: str | None
    attributes_json: dict | None
    variant_group_key: str | None
    volume_ml: int | None
    brand_id: int | None
    category_id: int | None
    brand: BrandResponse | None = None
    category: CategoryResponse | None = None

    @field_validator("reserved_quantity", mode="before")
    @classmethod
    def default_unpersisted_reserved_quantity(cls, value: int | None) -> int:
        return value or 0

    @field_validator("allow_backorder", mode="before")
    @classmethod
    def default_unpersisted_allow_backorder(cls, value: bool | None) -> bool:
        return bool(value)


class PublicProductResponse(TimestampedResponse, _SanitizedProductDescriptions):
    id: int
    name: str
    name_en: str | None = None
    old_name: str | None = None
    model_name: str | None = None
    product_type: str | None = None
    product_type_en: str | None = None
    package_size: str | None = None
    slug: str
    description: str | None
    description_en: str | None = None
    ingredients: str | None = None
    ingredients_en: str | None = None
    short_description: str | None
    short_description_en: str | None = None
    price: Decimal
    recommended_retail_price: Decimal | None
    sku: str | None
    is_active: bool
    image_url: str | None
    external_url: str | None
    availability_status: str | None
    attributes_json: dict | None
    variant_group_key: str | None
    volume_ml: int | None
    brand_id: int | None
    category_id: int | None
    brand: BrandResponse | None = None
    category: CategoryResponse | None = None


class ProductImageResponse(TimestampedResponse):
    id: int
    product_id: int
    upload_id: int | None = None
    image_url: str | None = None
    alt: str | None = None
    sort_order: int
    is_active: bool


class ProductImageVariantResponse(TimestampedResponse):
    id: int
    product_id: int
    source_image_id: int | None = None
    source_url: str
    source_fingerprint: str
    preset: str
    recipe_version: str
    processor_version: str | None = None
    processing_config: dict | None = None
    status: str
    error_detail: str | None = None
    attempt_count: int
    output_upload_id: int | None = None
    output_url: str | None = None
    is_preferred: bool


class BackofficeProductResponse(ProductResponse):
    display_image_url: str | None = None
    display_image_urls: list[str] = Field(default_factory=list)
    is_effectively_visible: bool
    hidden_reason: HiddenReason | None
    images: list[ProductImageResponse]


class ProductImageUpdate(BaseModel):
    alt: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class ProductImageReorderRequest(BaseModel):
    image_ids: list[int]


class CategoryPathItem(BaseModel):
    id: int
    name: str
    name_en: str | None = None
    slug: str


class ProductVolumeVariantResponse(BaseModel):
    id: int
    name: str
    name_en: str | None = None
    slug: str
    sku: str | None = None
    volume_ml: int
    volume_label: str
    price: Decimal
    base_price: Decimal
    compare_at_price: Decimal | None = None
    image_url: str | None = None
    availability_status: str | None = None
    is_available: bool


class ShopProductResponse(PublicProductResponse):
    is_effectively_visible: bool
    hidden_reason: HiddenReason | None
    is_available_for_purchase: bool
    base_price: Decimal
    images: list[str] = Field(
        default_factory=list,
        description="Active gallery URLs in display order: up to 3 in catalog/search lists, full gallery in product details.",
    )
    category_tree: list[CategoryPathItem] = Field(default_factory=list)
    compare_at_price: Decimal | None = None
    discount_percent: Decimal | None = None
    discount_amount: Decimal = Decimal("0.00")
    promotion_id: int | None = None
    promotion_name: str | None = None
    promotion_code: str | None = None
    is_new: bool = False
    is_top: bool = False
    average_rating: Decimal | None = None
    reviews_count: int = 0
    volume_variants: list[ProductVolumeVariantResponse] = Field(default_factory=list)


class ProductViewResponse(BaseModel):
    recorded: bool
    viewed_on: date


class ProductSearchResponse(BaseModel):
    suggestions: list[str] = Field(default_factory=list)
    products: list[ShopProductResponse] = Field(default_factory=list)
    categories: list[CategoryResponse] = Field(default_factory=list)


class FilterValueResponse(BaseModel):
    slug: str
    name: str
    count: int


class FilterGroupResponse(BaseModel):
    slug: str
    name: str
    values: list[FilterValueResponse] = Field(default_factory=list)


class PriceRangeResponse(BaseModel):
    min: Decimal | None = None
    max: Decimal | None = None


class CategoryFiltersResponse(BaseModel):
    price: PriceRangeResponse
    filters: dict[str, FilterGroupResponse] = Field(default_factory=dict)


class ProductReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5, strict=True)
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment", mode="before")
    @classmethod
    def normalize_comment(cls, value):
        return message_input(value, 2000, 3)


class ProductReviewCommentCreate(BaseModel):
    comment: str = Field(min_length=3, max_length=2000)

    @field_validator("comment", mode="before")
    @classmethod
    def normalize_comment(cls, value):
        return message_input(value, 2000, 3)


class ProductReviewCommentResponse(TimestampedResponse):
    id: int
    review_id: int
    customer_id: int
    customer_name: str | None = None
    comment: str


class ProductReviewResponse(TimestampedResponse):
    id: int
    product_id: int
    customer_id: int
    customer_name: str | None = None
    rating: int
    comment: str | None = None
    comments_count: int = 0


class ProductReviewListResponse(BaseModel):
    total: int
    average_rating: Decimal | None = None
    items: list[ProductReviewResponse] = Field(default_factory=list)


class DeliveryListResponse(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    cached: bool = False
    updated_at: datetime | None = None
