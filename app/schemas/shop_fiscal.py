from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionCommand(StrictCommand):
    expected_revision: int = Field(ge=1, strict=True)


class ShiftCommand(StrictCommand):
    expected_shift_revision: int = Field(ge=1, strict=True)
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class ApproveCommand(RevisionCommand):
    preview_id: int = Field(gt=0, strict=True)
    confirm: Literal[True]
    _confirmation = field_validator("confirm", mode="before")(ShiftCommand.explicit_confirmation.__func__)


class EvidenceCommand(RevisionCommand):
    evidence: str = Field(min_length=1, max_length=500)

    @field_validator("evidence")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Nonblank evidence required")
        return value.strip()


class ReturnLine(StrictCommand):
    order_item_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, strict=True)


class ReturnCommand(RevisionCommand):
    sale_document_id: int = Field(gt=0, strict=True)
    carrier_receipt_id: int | None = Field(default=None, gt=0, strict=True)
    lines: list[ReturnLine] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    _reason = field_validator("reason")(EvidenceCommand.nonblank.__func__)


class RefundCommand(EvidenceCommand):
    external_reference: str = Field(min_length=1, max_length=200)
    _reference = field_validator("external_reference")(EvidenceCommand.nonblank.__func__)


class ReceiptLine(ReturnLine):
    disposition: Literal["restock", "not_restock"]


class ReceiptCommand(EvidenceCommand):
    lines: list[ReceiptLine] = Field(min_length=1, max_length=200)


NOTICE = "Локальна тестова фіскалізація. Не є фіскальним чеком. До ДПС не надсилається."


class TestView(BaseModel):
    authority: Literal["test"] = "test"
    simulation: Literal[True] = True
    is_sent_dps: Literal[False] = False
    notice: str = NOTICE


class PreviewView(TestView):
    id: int
    order_id: int
    return_id: int | None
    kind: Literal["sale", "return"]
    expected_revision: int
    shift_revision: int
    snapshot: dict
    snapshot_hash: str
    expires_at: datetime


class DocumentView(TestView):
    id: int
    order_id: int
    return_id: int | None
    kind: Literal["sale", "return"]
    status: Literal["queued", "unknown", "issued", "cancelled", "error"]
    revision: int
    provider_uuid: str
    shift_revision: int
    amount_minor: int
    currency: Literal["UAH"] = "UAH"
    snapshot: dict
    snapshot_hash: str
    dispatched_at: datetime | None
    issued_at: datetime | None
    created_at: datetime
    actions: dict[str, bool]
    blocking_reasons: list[str]


class RefundView(TestView):
    id: int
    amount_minor: int
    method: str
    external_reference: str
    evidence: str
    created_at: datetime


class PhysicalReceiptView(TestView):
    id: int
    carrier_receipt_id: int | None = None
    lines: list[dict]
    evidence: str
    created_at: datetime


class ReturnView(TestView):
    id: int
    order_id: int
    sale_document_id: int
    carrier_receipt_id: int | None = None
    revision: int
    shift_revision: int
    amount_minor: int
    currency: Literal["UAH"] = "UAH"
    lines: list[dict]
    reason: str
    refund: RefundView | None
    physical_receipt: PhysicalReceiptView | None
    fiscal_document: DocumentView | None
    created_at: datetime
    actions: dict[str, bool]
    blocking_reasons: list[str]


class Reconciliation(BaseModel):
    pending_document_ids: list[int]
    unreceipted_payment_ids: list[int]
    unresolved_return_ids: list[int]
    unknown_document_ids: list[int]
    carrier_return_case_ids: list[int] = Field(default_factory=list)


class ReportView(TestView):
    id: int
    kind: Literal["x", "z"]
    register_id: str = "SHOP-MOCK-KASA-1"
    shift_revision: int
    totals: dict
    reconciliation: Reconciliation
    created_at: datetime


class RegisterView(TestView):
    id: str = "SHOP-MOCK-KASA-1"
    enabled: bool
    shift_status: str
    shift_revision: int
    actions: dict[str, bool]
    blocking_reasons: list[str]
    reconciliation: Reconciliation | None
    reports: list[ReportView]


class OrderFiscalView(TestView):
    enabled: bool
    order_id: int
    revision: int
    sale_document: DocumentView | None
    returns: list[ReturnView]
    actions: dict[str, bool]
    blocking_reasons: list[str]
