from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class OnlinePaymentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnlinePaymentRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    invoice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class OnlinePaymentMethodCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_attempt_id: int | None = Field(ge=1)
    payment_method: Literal["cash", "cashOnDelivery"]


class OnlinePaymentActions(BaseModel):
    start: bool = False
    continue_payment: bool = False
    refresh: bool = True
    retry: bool = False
    change_method: bool = False
    contact_support: bool = False


class OnlinePaymentOrder(BaseModel):
    id: int
    number: str
    status: str
    fulfillment_method: str | None
    item_count: int
    payment_method: str | None


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
    reason: str = "payment_review_required"
    actions: OnlinePaymentActions = Field(default_factory=OnlinePaymentActions)
    available_payment_methods: list[Literal["cash", "cashOnDelivery"]] = Field(default_factory=list)
    revision: int = 1
    attempt_id: int | None = None
    order: OnlinePaymentOrder | None = None
