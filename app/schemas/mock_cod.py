"""Local mock COD commands never accept a client-supplied payment amount."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class MockCarrierCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, strict=True)
    evidence: str = Field(min_length=1, max_length=500)
    confirm: StrictBool

    @field_validator('evidence')
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError('Evidence required')
        return value.strip()

    @field_validator('confirm')
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError('Explicit mock confirmation required')
        return value


class MockCODPaymentCreate(MockCarrierCommand):
    provider_event_id: str = Field(pattern=r'^MOCK-[A-Za-z0-9_-]{1,123}$', max_length=128)


class MockParcelQuantity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_item_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, strict=True)


class MockReturnShipmentCreate(MockCarrierCommand):
    items: list[MockParcelQuantity] | None = Field(default=None, min_length=1, max_length=200)


class MockParcelReceiptItem(MockParcelQuantity):
    disposition: Literal['restock', 'not_restock']


class MockParcelReceiptCreate(MockCarrierCommand):
    items: list[MockParcelReceiptItem] = Field(min_length=1, max_length=200)


class MockParcelItem(BaseModel):
    order_item_id: int
    product_id: int
    name: str | None
    quantity: int


class MockCODPaymentResponse(BaseModel):
    id: int
    payment_id: int
    provider_event_id: str
    amount_minor: int
    evidence: str
    operator_id: int
    created_at: datetime


class MockParcelReceiptResponse(BaseModel):
    id: int
    tracking_number: str
    items: list[MockParcelReceiptItem]
    evidence: str
    operator_id: int
    created_at: datetime


class MockSettlementResponse(BaseModel):
    mode: Literal['mock_only'] = 'mock_only'
    available: bool
    order_id: int
    shipment_id: int
    tracking_number: str | None
    revision: int
    amount_minor: int
    cod_payment: MockCODPaymentResponse | None
    return_receipt: MockParcelReceiptResponse | None
    items: list[MockParcelItem]
    allowed_actions: list[Literal['confirm_cod', 'receive_return', 'create_return_shipment']]
    blocking_reasons: list[str]
