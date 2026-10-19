from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator


class FiscalCommandBase(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FiscalPreviewCreate(FiscalCommandBase):
    cash_register_id: Literal['MOCK-KASA-1']


class MockShiftCommand(FiscalCommandBase):
    expected_shift_revision: StrictInt = Field(ge=0)


class FiscalSubmissionCreate(FiscalPreviewCreate):
    preview_id: StrictInt = Field(gt=0)
    expected_revision: StrictInt = Field(ge=1)
    snapshot_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirm: StrictBool
    mock_outcome: Literal['accepted', 'timeout_after_acceptance'] = 'accepted'

    @field_validator('confirm')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation required')
        return value


class FiscalCommand(FiscalCommandBase):
    expected_revision: StrictInt = Field(ge=1)


class FiscalProcessCommand(FiscalCommand):
    mock_delivery_outcome: Literal['delivered', 'failed'] = 'delivered'


class FiscalDeliveryCommand(FiscalCommand):
    mock_outcome: Literal['delivered', 'failed'] = 'delivered'


class MockFiscalInboxCreate(FiscalCommandBase):
    provider_operation_id: UUID
    event_id: UUID
    status: Literal['issued', 'processing', 'unknown']


class MockRegisterResponse(BaseModel):
    id: str
    label: str
    credential_kind: Literal['mock'] = 'mock'
    shift_status: Literal['closed', 'open']
    shift_revision: int


class FiscalReadinessResponse(BaseModel):
    mode: Literal['mock_only'] = 'mock_only'
    available: bool
    blocking_reasons: list[str]
    cash_registers: list[MockRegisterResponse]


class FiscalPreviewResponse(BaseModel):
    kind: Literal['sale', 'return'] = 'sale'
    parent_sale_id: int | None = None
    return_id: int | None = None
    id: int
    order_id: int
    revision: int
    cash_register_id: str
    shift_revision: int
    snapshot_hash: str
    snapshot: dict[str, Any]
    expires_at: datetime
    simulation: Literal[True] = True
    document_kind: Literal['mock_fiscal_receipt'] = 'mock_fiscal_receipt'
    is_sent_dps: Literal[False] = False
    allowed_actions: list[str]
    blocking_reasons: list[str]


class FiscalDeliveryResponse(BaseModel):
    status: Literal['pending_document', 'queued', 'delivered', 'failed']
    channel: Literal['local_display_print'] = 'local_display_print'
    attempts: int
    last_error: str | None


class FiscalDocumentResponse(BaseModel):
    automation_id: int | None = None
    qr_attempt_id: int | None = None
    source: Literal['manual', 'automation', 'qr'] = 'manual'
    kind: Literal['sale', 'return'] = 'sale'
    parent_sale_id: int | None = None
    return_id: int | None = None
    id: int
    order_id: int
    status: Literal['queued', 'submitting', 'unknown', 'processing', 'issued', 'cancelled', 'error']
    revision: int
    simulation: Literal[True] = True
    document_kind: Literal['mock_fiscal_receipt'] = 'mock_fiscal_receipt'
    provider_uuid: str
    cash_register_id: str
    shift_revision: int
    snapshot_hash: str
    snapshot: dict[str, Any]
    is_sent_dps: Literal[False] = False
    delivery: FiscalDeliveryResponse
    allowed_actions: list[str]
    blocking_reasons: list[str]
    print_url: str | None
