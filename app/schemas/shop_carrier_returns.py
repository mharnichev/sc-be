from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from app.schemas.shop_fiscal import RevisionCommand, ReturnLine, EvidenceCommand, ReceiptLine, ShiftCommand


class CarrierCreate(EvidenceCommand):
    original_shipment_id: int = Field(gt=0, strict=True)
    lines: list[ReturnLine] = Field(min_length=1, max_length=200)
    confirm: Literal[True]
    _confirm = field_validator("confirm", mode="before")(ShiftCommand.explicit_confirmation.__func__)


class CarrierReceive(EvidenceCommand):
    lines: list[ReceiptLine] = Field(min_length=1, max_length=200)
    confirm: Literal[True]
    _confirm = field_validator("confirm", mode="before")(ShiftCommand.explicit_confirmation.__func__)


class CarrierClose(EvidenceCommand):
    confirm: Literal[True]
    _confirm = field_validator("confirm", mode="before")(ShiftCommand.explicit_confirmation.__func__)


class TestCarrierView(BaseModel):
    mode: Literal["shop_mock_carrier_return"] = "shop_mock_carrier_return"
    authority: Literal["test"] = "test"
    simulation: Literal[True] = True
    notice: str = "Локальне тестове повернення НП. Реальних операцій перевізника немає."


class CarrierObservationView(BaseModel):
    status: str | None
    event_at: datetime | None
    observed_at: datetime | None
    last_attempt_status: str


class CarrierReceiptView(TestCarrierView):
    id: int
    lines: list[dict]
    evidence: str
    created_at: datetime


class CarrierClosureView(TestCarrierView):
    id: int
    evidence: str
    created_at: datetime


class CarrierCaseView(TestCarrierView):
    id: int
    order_id: int
    revision: int
    order_revision: int
    original_shipment_id: int
    return_shipment_id: int
    tracking_number: str | None
    provider_uuid: str
    status: Literal["unknown", "returning", "returned", "received", "closed_unpaid"]
    financial_mode: Literal["paid", "unpaid_cod"]
    shift_revision: int | None
    sale_document_id: int | None
    lines: list[dict]
    observation: CarrierObservationView | None
    receipt: CarrierReceiptView | None
    closure: CarrierClosureView | None
    financial_remaining_lines: list[dict]
    actions: dict[str, bool]
    blocking_reasons: list[str]


class CarrierStateView(TestCarrierView):
    order_id: int
    revision: int
    enabled: bool
    original_shipment_id: int | None
    financial_mode: Literal["paid", "unpaid_cod"] | None
    eligible_lines: list[dict]
    cases: list[CarrierCaseView]
    actions: dict[str, bool]
    blocking_reasons: list[str]
