from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.order import BackofficeOrderResponse


class ShopCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class ManualShopPayment(ShopCommand):
    amount_minor: int = Field(gt=0, strict=True)
    currency: Literal["UAH"]
    method: Literal["cash", "card", "bank_transfer", "cod"]
    external_reference: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=500)

    @field_validator("external_reference", "evidence")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("A nonblank value is required")
        return value.strip()


class ProviderShopPayment(ShopCommand):
    reference: str = Field(min_length=1, max_length=200)

    @field_validator("reference")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("A nonblank reference is required")
        return value.strip()


class ShopPhysicalCommand(ShopCommand):
    evidence: str = Field(min_length=1, max_length=500)

    @field_validator("evidence")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Nonblank evidence is required")
        return value.strip()


class ShopPaymentView(BaseModel):
    id: int
    method: str
    source: str
    amount_minor: int
    currency: Literal["UAH"] = "UAH"
    external_reference: str
    authority: Literal["manual", "provider", "test"]
    created_at: datetime


class ShopFulfillmentView(BaseModel):
    id: int
    kind: Literal["pickup", "nova_poshta"]
    shipment_id: int | None
    tracking_number: str | None
    authority: Literal["production", "test"]
    created_at: datetime


class ShopCompletionView(BaseModel):
    id: int
    authority: Literal["production", "test"]
    created_at: datetime


class ShopOperationResponse(BaseModel):
    payments_profile: Literal["r2", "legacy"] = "legacy"
    fiscal_profile: Literal["checkbox", "mono_checkbox_external", "local_mock", "unavailable"] = "unavailable"
    order_id: int
    revision: int
    order: BackofficeOrderResponse
    stage: Literal["reserved", "paid", "dispatched", "completed"]
    authority: Literal["none", "manual", "provider", "test"]
    payment: ShopPaymentView | None
    fulfillment: ShopFulfillmentView | None
    completion: ShopCompletionView | None
    actions: dict[str, bool]
    blocking_reasons: list[str]
    available_payment_methods: list[str]
