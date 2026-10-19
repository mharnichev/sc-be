"""Private backoffice contracts for inventory operations.

These schemas deliberately live outside the public product contracts: stock,
purchase cost, allocation, and procurement data must never be exposed by the
shop API.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import ORMModel, TimestampedResponse


class InventoryProductResponse(ORMModel):
    id: int
    name: str
    sku: str | None = None
    barcode: str | None = None
    on_hand: int = Field(validation_alias=AliasChoices("on_hand", "stock_quantity"))
    reserved: int = Field(validation_alias=AliasChoices("reserved", "reserved_quantity"))
    available: int = Field(validation_alias=AliasChoices("available", "available_quantity"))
    allow_backorder: bool
    brand_id: int | None = None
    brand_name: str | None = Field(default=None, validation_alias=AliasPath("brand", "name"))
    category_id: int | None = None
    model_name: str | None = None
    package_size: str | None = None
    volume_ml: int | None = None
    image_url: str | None = None
    display_image_url: str | None = None
    display_image_urls: list[str] = Field(default_factory=list)
    created_by_user_id: int | None = None
    barcode_bound_by_user_id: int | None = None


class BarcodeBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    barcode: str = Field(min_length=1, max_length=64)

    @field_validator("barcode")
    @classmethod
    def non_blank_barcode(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Barcode must be a non-blank string")
        return value


class InventoryProductCreateRequest(BarcodeBindRequest):
    name: str = Field(min_length=2, max_length=255)
    sku: str | None = Field(default=None, min_length=1, max_length=100)
    brand_id: int | None = Field(default=None, gt=0)
    category_id: int | None = Field(default=None, gt=0)
    model_name: str | None = Field(default=None, max_length=255)
    package_size: str | None = Field(default=None, max_length=64)
    volume_ml: int | None = Field(default=None, gt=0)
    image_url: str | None = Field(default=None, max_length=500)
    price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)

    @field_validator("name")
    @classmethod
    def non_blank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Name must be non-blank")
        return value


class ProductInventorySettingsUpdate(BaseModel):
    barcode: str | None = Field(default=None, max_length=64)
    allow_backorder: bool


class ProcurementQueueItemResponse(ORMModel):
    product_id: int
    product_name: str
    sku: str | None = None
    barcode: str | None = None
    total_quantity_required: int
    requirements: list[dict[str, int | str]] = Field(default_factory=list)


class MarkOrderedRequest(BaseModel):
    order_item_ids: list[int] = Field(min_length=1)


class ReceiptCreateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=255)
    comment: str | None = Field(default=None, max_length=2000)


class ReceiptAllocationRequest(ORMModel):
    order_item_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class ReceiptItemCreateRequest(BaseModel):
    product_id: int | None = Field(default=None, gt=0)
    barcode: str | None = Field(default=None, min_length=1, max_length=64)
    quantity: int = Field(gt=0)
    purchase_unit_cost: Decimal = Field(ge=0)
    batch_number: str | None = Field(default=None, max_length=100)
    expiration_date: date | None = None
    allocations: list[ReceiptAllocationRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identifier(self) -> "ReceiptItemCreateRequest":
        if (self.product_id is None) == (self.barcode is None):
            raise ValueError("Provide exactly one of product_id or barcode")
        return self


class ReceiptItemResponse(ORMModel):
    id: int
    product_id: int
    barcode: str | None = None
    quantity: int
    purchase_unit_cost: Decimal = Field(validation_alias=AliasChoices("purchase_unit_cost", "purchase_cost"))
    batch_number: str | None = Field(default=None, validation_alias=AliasChoices("batch_number", "lot_number"))
    expiration_date: date | None = Field(default=None, validation_alias=AliasChoices("expiration_date", "expires_at"))
    allocations: list[ReceiptAllocationRequest] = Field(default_factory=list)


class ReceiptResponse(TimestampedResponse):
    id: int
    posted_by_user_id: int | None = None
    reason: str
    comment: str | None = None
    status: str
    posted_at: datetime | None = None
    items: list[ReceiptItemResponse] = Field(default_factory=list)


class ManualOperationRequest(BaseModel):
    product_id: int | None = Field(default=None, gt=0)
    barcode: str | None = Field(default=None, min_length=1, max_length=64)
    quantity: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=255)
    order_id: int | None = Field(default=None, gt=0)
    order_item_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_identifier(self) -> "ManualOperationRequest":
        if (self.product_id is None) == (self.barcode is None):
            raise ValueError("Provide exactly one of product_id or barcode")
        return self


class InventoryMovementResponse(ORMModel):
    id: int
    product_id: int
    movement_type: str
    quantity: int
    on_hand_delta: int
    reserved_delta: int
    reason: str | None = None
    order_id: int | None = None
    order_item_id: int | None = None
    admin_user_id: int | None = Field(default=None, validation_alias=AliasChoices("admin_user_id", "performed_by_user_id"))
    receipt_id: int | None = None
    count_id: int | None = Field(default=None, validation_alias=AliasChoices("count_id", "inventory_count_id"))
    created_at: datetime


class CountCreateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=255)


class CountItemUpsertRequest(BaseModel):
    product_id: int | None = Field(default=None, gt=0)
    barcode: str | None = Field(default=None, min_length=1, max_length=64)
    counted_quantity: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_identifier(self) -> "CountItemUpsertRequest":
        if (self.product_id is None) == (self.barcode is None):
            raise ValueError("Provide exactly one of product_id or barcode")
        return self


class CountItemResponse(ORMModel):
    id: int
    product_id: int
    barcode: str | None = None
    expected_quantity: int | None = None
    counted_quantity: int
    difference: int | None = None


class CountResponse(TimestampedResponse):
    id: int
    reason: str
    status: str
    posted_at: datetime | None = None
    items: list[CountItemResponse] = Field(default_factory=list)


class InventoryPermissionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    can_post_inventory: bool


class InventoryPermissionResponse(BaseModel):
    user_id: int
    can_post_inventory: bool
