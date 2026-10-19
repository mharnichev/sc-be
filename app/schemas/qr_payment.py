from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import Field, StrictBool, StrictInt, field_validator
from app.schemas.fiscal import FiscalCommandBase


class QRCreate(FiscalCommandBase):
    expected_revision: StrictInt = Field(ge=1)
    cash_register_id: Literal['MOCK-KASA-1']
    confirm: StrictBool

    @field_validator('confirm')
    @classmethod
    def confirmed(cls, value):
        if not value:
            raise ValueError('Explicit mock payment consent required')
        return value


class QRCommandCreate(FiscalCommandBase):
    expected_revision: StrictInt = Field(ge=1)


class QRSimulate(QRCommandCreate):
    event_id: UUID
    outcome: Literal['success', 'decline', 'expiry', 'processing', 'hold', 'timeout_after_acceptance',
                     'success_receipt_error', 'success_receipt_pending', 'receipt_issued']


class QRAttemptResponse(FiscalCommandBase):
    id: int
    order_id: int
    revision: int
    provider_invoice_id: str
    payment_status: Literal['waiting','processing','unknown','paid','declined','expired','cancelled']
    receipt_status: Literal['pending','issued','error']
    amount_minor: int
    currency: Literal['UAH'] = 'UAH'
    mode: Literal['mock_only', 'provider_sandbox'] = 'mock_only'
    payment_url: str | None = None
    checkbox_receipt_id: str | None = None
    verification: dict | None = None
    simulation: Literal[True] = True
    expires_at: datetime
    snapshot: dict
    local_payment_path: str
    payment_id: int | None
    fiscal_document_id: int | None
    receipt_url: str | None
    allowed_actions: list[str]
    blocking_reasons: list[str]
