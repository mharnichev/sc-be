from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class MasterMonoCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class MasterMonoRecover(MasterMonoCommand):
    invoice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$", strict=True)


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
    receipt_pdf_path: str | None = None
    can_handover: bool
    blocking_reasons: list[str]
