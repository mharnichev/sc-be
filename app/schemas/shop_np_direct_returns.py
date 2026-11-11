from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.shop_fiscal import ReturnLine
from app.schemas.shop_np_returns import ManualLink, NPCommand


class AttachCommand(NPCommand):
    original_shipment_id: int = Field(gt=0, strict=True)
    return_ttn: str = Field(pattern=r"^[0-9]{14}$")
    lines: list[ReturnLine] = Field(min_length=1, max_length=200)
    linkage_kind: Literal["easy_return", "manual_document"] = "easy_return"
    manual_link: ManualLink | None = None

    @model_validator(mode="after")
    def exact_link(self):
        if (self.linkage_kind == "manual_document") != bool(self.manual_link):
            raise ValueError("Manual linkage requires separate verified documentary evidence")
        return self


class ReceivedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_item_id: int = Field(gt=0, strict=True)
    received_quantity: int = Field(ge=0, strict=True)
    restock_quantity: int = Field(ge=0, strict=True)
    damaged_quantity: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def split(self):
        if self.restock_quantity + self.damaged_quantity != self.received_quantity:
            raise ValueError("Restock and damaged quantities must add up to the received quantity")
        return self


class ReceiveCommand(NPCommand):
    lines: list[ReceivedLine] = Field(min_length=1, max_length=200)
    contents_verified: Literal[True]
    evidence: str = Field(min_length=1, max_length=500)
    _verified = field_validator("contents_verified", mode="before")(ManualLink.explicit.__func__)
    _evidence = field_validator("evidence")(ManualLink.nonblank.__func__)


class ReceiptView(BaseModel):
    id: UUID
    proof_hash: str
    quantities: dict[str, int]
    lines: list[ReceivedLine]
    evidence: str
    operator_id: int
    created_at: datetime


class ReturnView(BaseModel):
    order_id: int
    revision: int
    available: bool
    profile_id: str | None
    profile_hash: str | None
    original_shipment_id: int | None
    original_ttn: str | None
    case_id: UUID | None
    return_ttn: str | None
    status: Literal["not_attached", "awaiting_return", "arrived", "received"]
    eligible_lines: list[ReturnLine]
    claimed_lines: list[ReturnLine]
    observation: dict | None
    receipt: ReceiptView | None
    financial_refund_path: str | None
    can_attach: bool
    can_refresh: bool
    can_receive: bool
    blocking_reasons: list[str]
