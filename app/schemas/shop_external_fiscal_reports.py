from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from app.schemas.shop_mono_checkbox_refunds import DirectFiscalReport

ReportKind = Literal["x", "z"]


class ReportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sale", "return"]
    receipt_id: UUID
    original_receipt_id: UUID | None
    amount_minor: StrictInt = Field(gt=0)


class SyntheticReportEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ReportKind
    authority: Literal["test"]
    scope: Literal["provider_cash_register_shift"]
    invoice_id: str = Field(min_length=1, max_length=128)
    merchant_id: str = Field(min_length=1, max_length=128)
    profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_id: UUID
    shift_id: UUID
    register_id: str = Field(min_length=1, max_length=128)
    reported_at: AwareDatetime
    currency: Literal["UAH"]
    shift_sale_minor: StrictInt = Field(ge=0)
    shift_return_minor: StrictInt = Field(ge=0)
    receipts: list[ReportReceipt] = Field(max_length=2)


class SyntheticReportBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reports: list[SyntheticReportEvidence] = Field(min_length=1, max_length=2)


class ProviderReportComparison(BaseModel):
    status: Literal["unavailable", "missing", "invalid", "conflict", "partial", "matched", "not_applicable"]
    evidence: SyntheticReportEvidence | None = None
    reports: list[SyntheticReportEvidence] = Field(default_factory=list)
    chronology_checked: bool = False
    missing_receipt_ids: list[UUID] = Field(default_factory=list)


class ExternalFiscalReconciliation(BaseModel):
    order_id: int
    revision: int
    local_status: Literal["not_applicable", "sale_unresolved", "projected"]
    local_projection: DirectFiscalReport | None = None
    refund_status: Literal["none", "unresolved", "finalized"]
    refund_reason: str | None = None
    provider_x: ProviderReportComparison
    provider_z: ProviderReportComparison
