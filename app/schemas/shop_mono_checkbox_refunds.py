from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class StartRefund(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=1)
    kind: Literal["full", "partial"]
    goods_in_custody: Literal[True]
    carrier_receipt_id: UUID | None = None
    quantities: dict[int, StrictInt] = Field(default_factory=dict, max_length=200)


class DirectFiscalEntry(BaseModel):
    kind: Literal["sale", "return"]
    receipt_id: str
    original_receipt_id: str | None = None
    amount_minor: int
    structured_totals_verified: bool


class DirectFiscalReport(BaseModel):
    issuer: Literal["mono_checkbox_external"] = "mono_checkbox_external"
    authority: Literal["test", "provider"]
    currency: Literal["UAH"] = "UAH"
    entries: list[DirectFiscalEntry]
    net_minor: int


class RefundableItem(BaseModel):
    basket_index: int
    name: str
    quantity: int
    unit_amount_minor: int


class DirectRefundState(BaseModel):
    order_id: int
    revision: int
    available: bool
    can_start: bool = False
    can_reconcile: bool = False
    carrier_receipt_id: UUID | None = None
    refundable_items: list[RefundableItem] = Field(default_factory=list)
    kind: Literal["full", "partial"] | None = None
    amount_minor: int | None = None
    ext_ref: str | None = None
    bank_confirmed: bool = False
    return_receipt_id: str | None = None
    finalized_at: datetime | None = None
    last_checked_at: datetime | None = None
    reason: str
    report: DirectFiscalReport | None = None
