from datetime import datetime
from typing import Any, Literal

from pydantic import Field, StrictInt

from app.schemas.fiscal import FiscalCommand, FiscalCommandBase, FiscalSubmissionCreate


class AutomationPreviewCreate(FiscalCommandBase):
    cash_register_id: Literal['MOCK-KASA-1']
    shipment_id: StrictInt = Field(gt=0)
    condition: Literal['mock_delivery_confirmed']


class AutomationCreate(FiscalSubmissionCreate):
    mock_outcome: Literal['accepted', 'timeout_before_acceptance', 'timeout_after_acceptance'] = 'accepted'


class AutomationCancel(FiscalCommand):
    mock_outcome: Literal['confirmed', 'timeout_before_acceptance', 'timeout_after_acceptance'] = 'confirmed'


class MockAutomationBase(FiscalCommandBase):
    mode: Literal['mock_only'] = 'mock_only'
    provider_contract: Literal['unknown'] = 'unknown'
    simulation: Literal[True] = True
    is_sent_dps: Literal[False] = False
    notice: str
    allowed_actions: list[str]
    blocking_reasons: list[str]


class AutomationPreviewResponse(MockAutomationBase):
    id: int
    order_id: int
    revision: int
    cash_register_id: str
    shift_revision: int
    snapshot_hash: str
    snapshot: dict[str, Any]
    expires_at: datetime


class AutomationResponse(MockAutomationBase):
    fiscal_document_id: int | None = None
    id: int
    order_id: int
    payment_id: int
    shipment_id: int
    tracking_number: str
    status: Literal['queued', 'unknown', 'armed', 'cancel_pending', 'cancel_unknown', 'cancelled', 'issued', 'error']
    revision: int
    provider_uuid: str
    external_id: str | None
    cash_register_id: str
    shift_revision: int
    condition: Literal['mock_delivery_confirmed']
    snapshot_hash: str
    snapshot: dict[str, Any]
    approved_at: datetime
    dispatched_at: datetime | None
    cancellation_confirmed_at: datetime | None
    result: dict[str, Any] | None


class AutomationListResponse(MockAutomationBase):
    available: bool
    owner: Literal['sc_manual', 'checkbox_ettn'] | None
    items: list[AutomationResponse]
