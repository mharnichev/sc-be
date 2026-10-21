from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class OnlinePaymentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnlinePaymentRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    invoice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class OnlinePaymentState(BaseModel):
    order_id: int
    enabled: bool
    status: Literal["not_started", "creation_unknown", "waiting", "paid_receipt_pending", "accepted", "requires_review"]
    paid: bool
    receipt_accepted: bool
    page_url: str | None = None
    receipt_url: str | None = None
    amount_minor: int
    currency: Literal["UAH"] = "UAH"
    retry_after_seconds: int = 30
