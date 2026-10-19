"""Separate Checkbox DTOs. Production profile is not evidence of live acceptance."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.shop_fiscal import (RevisionCommand, ApproveCommand, EvidenceCommand,
    ReturnLine, RefundCommand, ReceiptCommand, ShiftCommand)


class VerifyCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]
    expected_profile_id: str = Field(min_length=1, max_length=100, strict=True)
    _confirmation = field_validator("confirm", mode="before")(ShiftCommand.explicit_confirmation.__func__)


class RegisterCommand(RevisionCommand):
    confirm: Literal[True]
    expected_profile_id: str = Field(min_length=1, max_length=100, strict=True)
    _confirmation = field_validator("confirm", mode="before")(ShiftCommand.explicit_confirmation.__func__)


class ReconcileCommand(RevisionCommand):
    candidate_report_uuid: UUID | None = None


class ReturnCommand(RevisionCommand):
    sale_document_id: int = Field(gt=0, strict=True)
    lines: list[ReturnLine] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    _reason = field_validator("reason")(EvidenceCommand.nonblank.__func__)


class OperationView(BaseModel):
    id: int
    kind: str
    status: Literal["queued", "unknown", "pending", "accepted", "rejected", "cancelled"]
    revision: int
    authority: Literal["test", "provider"]
    transport: Literal["mock_http", "live"]
    order_id: int | None
    document_id: int | None
    provider_uuid: str | None
    shift_id: str | None
    snapshot_hash: str
    dispatched_at: datetime | None
    acceptance_verified: bool
    accepted_at: datetime | None
    result: dict | None
    actions: dict[str, bool]
    blocking_reasons: list[str]


class Reconciliation(BaseModel):
    pending_operation_ids: list[int]
    unreceipted_payment_ids: list[int]
    unresolved_return_ids: list[int]
    unresolved_np_return_ids: list[int] = []
    unresolved_payment_resolution_ids: list[int] = []


class RegisterView(BaseModel):
    id: str
    revision: int
    authority: Literal["test", "provider"]
    transport: Literal["mock_http", "live"]
    identity_verified: bool
    shift_id: str | None
    shift_status: Literal["closed", "open", "unknown"]
    pending_operation: OperationView | None
    actions: dict[str, bool]
    reconciliation: Reconciliation | None
    reports: list[OperationView]


class ReadinessView(BaseModel):
    profile: Literal["checkbox"] = "checkbox"
    profile_id: str | None
    configured: bool
    enabled: bool
    authority: Literal["test", "provider", "none"]
    transport: Literal["mock_http", "live", "unavailable"]
    identity_verified: bool
    policy_available: bool
    register: RegisterView | None
    actions: dict[str, bool]
    blocking_reasons: list[str]


class PreviewView(BaseModel):
    id: int
    order_id: int
    return_id: int | None
    kind: Literal["sale", "return"]
    expected_revision: int
    register_revision: int
    authority: Literal["test", "provider"]
    transport: Literal["mock_http", "live"]
    snapshot: dict
    snapshot_hash: str
    expires_at: datetime


class DocumentView(BaseModel):
    id: int
    order_id: int
    return_id: int | None
    kind: Literal["sale", "return"]
    status: Literal["queued", "unknown", "pending", "issued", "rejected", "cancelled"]
    revision: int
    provider_uuid: str
    shift_id: str
    authority: Literal["test", "provider"]
    transport: Literal["mock_http", "live"]
    is_sent_dps: bool | None
    acceptance_verified: bool
    amount_minor: int
    currency: Literal["UAH"] = "UAH"
    snapshot: dict
    snapshot_hash: str
    operation: OperationView | None
    created_at: datetime
    issued_at: datetime | None
    actions: dict[str, bool]
    blocking_reasons: list[str]


class ReturnView(BaseModel):
    id: int
    order_id: int
    sale_document_id: int
    carrier_receipt_id: int | None = None
    cancellation_case_id: int | None = None
    physical_not_required: bool = False
    revision: int
    authority: Literal["test", "provider"]
    transport: Literal["mock_http", "live"]
    amount_minor: int
    currency: Literal["UAH"] = "UAH"
    lines: list[dict]
    reason: str
    refund: dict | None
    physical_receipt: dict | None
    fiscal_document: DocumentView | None
    actions: dict[str, bool]
    blocking_reasons: list[str]


class OrderFiscalView(BaseModel):
    profile: Literal["checkbox"] = "checkbox"
    enabled: bool
    order_id: int
    revision: int
    authority: Literal["test", "provider", "none"]
    transport: Literal["mock_http", "live", "unavailable"]
    sale_document: DocumentView | None
    returns: list[ReturnView]
    actions: dict[str, bool]
    blocking_reasons: list[str]


class ProfileView(BaseModel):
    order_id: int
    fiscal_profile: Literal["checkbox", "local_mock", "unavailable"]
