from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class RefreshReceiptCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    expected_profile_id: str = Field(min_length=1, max_length=64)
    expected_profile_hash: str = Field(min_length=64, max_length=64)


class DirectReceiptState(BaseModel):
    order_id: int
    revision: int
    fiscal_profile: Literal["mono_checkbox_external"] = "mono_checkbox_external"
    enabled: bool
    paid: bool
    accepted: bool
    authority: Literal["none", "test", "provider"]
    status: str
    receipt: dict | None = None
    blocking_reasons: list[str]
    verification_scope: Literal["provider_invoice_association"] = "provider_invoice_association"
    structured_totals_verified: bool = False
    reports_imported: bool = False
    internal_receipt_issuance_allowed: bool = False
