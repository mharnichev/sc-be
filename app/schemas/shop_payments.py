from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.shop_sales import ShopCommand, ShopPaymentView


class InvoiceIntentCommand(ShopCommand):
    expected_profile_id: str = Field(min_length=1, max_length=64)
    expected_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class VerifyInvoiceCommand(InvoiceIntentCommand):
    invoice_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class CODEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["bank_statement", "carrier_settlement"]
    source: str = Field(min_length=1, max_length=100)
    issuer: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,100}$")
    account: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,100}$")
    document_id: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,100}$")
    line_id: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,100}$")
    gross_amount_minor: int = Field(gt=0, strict=True)
    fee_minor: int = Field(ge=0, strict=True)
    net_amount_minor: int = Field(ge=0, strict=True)
    currency: Literal["UAH"]
    settled_at: datetime
    tracking_number: str = Field(pattern=r"^\d{14}$")
    verified: Literal[True]

    @field_validator("verified", mode="before")
    @classmethod
    def strict_attestation(cls, value):
        if value is not True:
            raise ValueError("Explicit verified attestation must be true")
        return value

    @field_validator("source", "issuer", "account", "document_id", "line_id")
    @classmethod
    def normalized(cls, value):
        if not value.strip():
            raise ValueError("Nonblank settlement identity required")
        return value.strip()

    @model_validator(mode="after")
    def exact_settlement(self):
        if self.settled_at.tzinfo is None:
            raise ValueError("Settlement timestamp requires a timezone")
        if self.gross_amount_minor != self.fee_minor + self.net_amount_minor:
            raise ValueError("Gross must equal fees plus net settlement")
        return self


class ManualCODCommand(InvoiceIntentCommand):
    evidence: CODEvidence


class PaymentReadiness(BaseModel):
    enabled: bool
    provider_available: bool
    profile_id: str | None
    profile_hash: str | None
    authority: Literal["none", "test", "provider"]
    transport: Literal["unavailable", "mock_http", "https"]
    blocking_reasons: list[str]
    manual_policy: dict | None = None
    manual_policy_hash: str | None = None
    replay_actions: dict[str, bool] = {}


class PaymentIntentView(BaseModel):
    id: str
    reference: str
    amount_minor: int
    currency: Literal["UAH"] = "UAH"
    invoice_id: str | None
    effective_invoice_id: str | None = None
    correction_id: int | None = None
    status: Literal["awaiting_invoice", "awaiting_verification", "accepted"]


class PaymentState(BaseModel):
    enabled: bool
    test_context: bool
    profile_id: str | None
    profile_hash: str | None
    authority: Literal["none", "manual", "test", "provider"]
    order_id: int
    revision: int
    mode: Literal["prepayment", "cod"]
    intent: PaymentIntentView | None
    payment: ShopPaymentView | None
    evidence: dict | None = None
    actions: dict[str, bool]
    blocking_reasons: list[str]
    readiness: PaymentReadiness
    verification_status: str | None = None
