"""Private backoffice contracts for inventory operations.

These schemas deliberately live outside the public product contracts: stock,
purchase cost, allocation, and procurement data must never be exposed by the
shop API.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import AliasChoices, BaseModel, Field, model_validator

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
