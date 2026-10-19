"""Local mock return commands deliberately exclude client supplied money."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator
from app.schemas.fiscal import FiscalCommandBase


class ReturnItemCreate(FiscalCommandBase):
    order_item_id: StrictInt = Field(gt=0)
    quantity: StrictInt = Field(gt=0)


class ReturnCreate(FiscalCommandBase):
    parent_sale_id: StrictInt = Field(gt=0)
    items: list[ReturnItemCreate] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def meaningful_reason(cls, value):
        if not value.strip():
            raise ValueError('Reason required')
        return value.strip()


class EvidenceCommand(FiscalCommandBase):
    evidence: str = Field(min_length=1, max_length=500)

    @field_validator('evidence')
    @classmethod
    def meaningful_evidence(cls, value):
        if not value.strip():
            raise ValueError('Evidence required')
        return value.strip()


class MockRefundCreate(EvidenceCommand):
    method: Literal['cash', 'card', 'monobank_qr'] | None = None


class ReturnReceiptItem(ReturnItemCreate):
    disposition: Literal['restock', 'not_restock']


class ReturnReceiptCreate(EvidenceCommand):
    items: list[ReturnReceiptItem] = Field(min_length=1, max_length=200)


class ReturnItemResponse(BaseModel):
    order_item_id: int
    product_id: int
    name: str | None
    quantity: int
    total_minor: int


class MockRefundResponse(BaseModel):
    qr_attempt_id: int | None = None
    id: int
    amount_minor: int
    method: Literal['cash', 'card', 'monobank_qr']
    evidence: str
    operator_id: int
    created_at: datetime


class ReturnReceiptResponse(BaseModel):
    shipment_return_receipt_id: int | None = None
    id: int
    items: list[ReturnReceiptItem]
    evidence: str
    operator_id: int
    created_at: datetime


class ReturnResponse(BaseModel):
    id: int
    order_id: int
    parent_sale_id: int
    amount_minor: int
    currency: Literal['UAH'] = 'UAH'
    items: list[ReturnItemResponse]
    reason: str
    simulation: Literal[True] = True
    created_at: datetime
    refund: MockRefundResponse | None
    receipt: ReturnReceiptResponse | None
    fiscal_document_ids: list[int]
    allowed_actions: list[str]
    blocking_reasons: list[str]


class MockReportCreate(FiscalCommandBase):
    kind: Literal['x', 'z']
    expected_shift_revision: StrictInt = Field(ge=0)
    confirm: StrictBool

    @field_validator('confirm')
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation required')
        return value


class ReportTotals(BaseModel):
    sale_count: int
    return_count: int
    sales_minor: int
    returns_minor: int
    net_minor: int
    document_ids: list[int]


class MockReportResponse(BaseModel):
    id: int
    kind: Literal['x', 'z']
    cash_register_id: str
    shift_revision: int
    totals: ReportTotals
    created_at: datetime
    simulation: Literal[True] = True
    notice: str


CaseType = Literal['payments_without_receipts', 'receipts_without_basis', 'unknown', 'refund_pending', 'fiscal_return_pending', 'receipt_pending', 'open_shifts', 'automation_receipt_missing']


class ReconciliationAckCreate(EvidenceCommand):
    case_type: CaseType
    resource_id: str = Field(min_length=1, max_length=64)


class ReconciliationAckResponse(BaseModel):
    id: int
    case_type: CaseType
    resource_id: str
    evidence: str
    actor_id: int
    created_at: datetime


class ReconciliationCase(BaseModel):
    resource_id: str
    order_id: int | None = None
    amount_minor: int = 0
    acknowledgements: list[ReconciliationAckResponse]


class ReconciliationTotals(BaseModel):
    sales_minor: int
    reserved_returns_minor: int
    refunds_minor: int
    fiscal_returns_minor: int


class ReconciliationResponse(BaseModel):
    simulation: Literal[True] = True
    notice: str
    buckets: dict[CaseType, list[ReconciliationCase]]
    totals: ReconciliationTotals
    acknowledgements: list[ReconciliationAckResponse]


class FiscalShareCreate(FiscalCommandBase):
    expires_in_seconds: StrictInt = Field(default=3600, ge=60, le=86400)


class FiscalShareRevoke(FiscalCommandBase):
    pass


class FiscalShareResponse(BaseModel):
    document_id: int
    generation: int
    url: str | None
    expires_at: datetime | None
    revoked: bool


class PublicMockFiscalItem(BaseModel):
    name: str | None
    quantity: int
    total_minor: int


class PublicMockFiscalDocument(BaseModel):
    kind: Literal['sale', 'return']
    items: list[PublicMockFiscalItem]
    amount_minor: int
    currency: Literal['UAH'] = 'UAH'
    notice: str
    simulation: Literal[True] = True
    is_sent_dps: Literal[False] = False
