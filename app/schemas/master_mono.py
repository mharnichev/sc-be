from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


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
