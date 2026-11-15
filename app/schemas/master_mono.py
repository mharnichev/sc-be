from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


MasterPaymentURLSource = Literal["mono_authenticated_create_v1"]


class MasterMonoCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class MasterMonoRecover(MasterMonoCommand):
    invoice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$", strict=True)


class MasterMonoCloseResult(BaseModel):
    order_id: int
    revision: int
    status: Literal["cancelled"] = "cancelled"


class MasterMonoClosedRecheckResult(BaseModel):
    order_id: int
    revision: int
    status: Literal["cancelled", "reconciliation_required"]


class MasterMonoState(BaseModel):
    enabled: bool
    order_id: int
    revision: int
    mode: Literal["mono_checkbox"] = "mono_checkbox"
    paid: bool
    receipt_accepted: bool
    status: str
    amount_minor: int
    invoice_id: str | None = None
    payment_url: str | None = None
    payment_url_source: MasterPaymentURLSource | None = None
    payment_url_expires_at: datetime | None = None
    receipt_pdf_path: str | None = None
    can_handover: bool
    can_verify_invoice: bool = False
    blocking_reasons: list[str]


class MasterMonoRefundCommand(MasterMonoCommand):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class MasterMonoRefundItem(BaseModel):
    name: str
    quantity: int


class MasterMonoRefundState(BaseModel):
    revision: int
    amount_minor: int
    items: list[MasterMonoRefundItem]
    money_status: Literal["not_started", "requires_review", "refunded"]
    receipt_status: Literal["not_started", "pending", "issued"]
    stock_status: Literal["not_required", "reserve_pending", "awaiting_goods", "received"]
    receipt_url: str | None
    can_start: bool
    can_reconcile: bool
    can_receive: bool
    reason: str | None
