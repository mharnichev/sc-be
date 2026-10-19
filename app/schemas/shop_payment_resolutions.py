"""Explicit pre-handover cancellation and bank-document resolution commands."""
from datetime import datetime
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.shop_sales import ShopCommand

Identifier = str
class ResolutionCommand(ShopCommand):
    expected_profile_id: str = Field(min_length=1,max_length=100)
    expected_profile_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

class OpenCommand(ResolutionCommand):
    reason: str = Field(min_length=1,max_length=500)
    goods_in_custody: Literal[True]
    @field_validator('goods_in_custody',mode='before')
    @classmethod
    def explicit(cls,value):
        if value is not True: raise ValueError('An explicit true attestation is required')
        return value
    @field_validator('reason')
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError('Nonblank evidence is required')
        return value.strip()

class InspectCommand(ResolutionCommand):
    invoice_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')

class CorrectCommand(ResolutionCommand):
    foreign_observation_id: int = Field(gt=0,strict=True)
    replacement_observation_id: int = Field(gt=0,strict=True)

class ClaimCommand(ResolutionCommand):
    reason: str = Field(min_length=1,max_length=500)
    _reason=field_validator('reason')(OpenCommand.nonblank.__func__)

class BankClosureEvidence(BaseModel):
    model_config=ConfigDict(extra='forbid')
    type: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    source: str = Field(min_length=1,max_length=100)
    issuer: str = Field(pattern=r'^[A-Za-z0-9_.:/-]{1,100}$')
    account: str = Field(pattern=r'^[A-Za-z0-9_.:/-]{1,100}$')
    document_id: str = Field(pattern=r'^[A-Za-z0-9_.:/-]{1,100}$')
    line_id: str = Field(pattern=r'^[A-Za-z0-9_.:/-]{1,100}$')
    order_id: int = Field(gt=0,strict=True)
    intent_id: str = Field(min_length=36,max_length=36)
    reference: str = Field(min_length=36,max_length=36)
    known_invoice_ids: list[Annotated[str,Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]] = Field(max_length=100)
    verified_at: datetime
    verified: Literal[True]
    no_money: Literal[True]
    no_hold: Literal[True]
    all_reference_invoices_closed: Literal[True]
    _true=field_validator('verified','no_money','no_hold','all_reference_invoices_closed',mode='before')(OpenCommand.explicit.__func__)
    _source=field_validator('source')(OpenCommand.nonblank.__func__)
    @model_validator(mode='after')
    def dated(self):
        if self.verified_at.tzinfo is None: raise ValueError('Timezone required')
        if len(set(self.known_invoice_ids))!=len(self.known_invoice_ids):raise ValueError('Duplicate invoice')
        return self

class CloseUnpaidCommand(ResolutionCommand):
    expected_manual_policy_id: str = Field(min_length=1,max_length=64)
    expected_manual_policy_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence: BankClosureEvidence

class FinalizeCommand(ResolutionCommand):
    confirm: Literal[True]
    _confirm=field_validator('confirm',mode='before')(OpenCommand.explicit.__func__)

class CaseView(BaseModel):
    id: int
    kind: Literal['paid_cancellation','invoice_review']
    order_id: int
    revision: int
    order_revision: int
    profile_id: str
    profile_hash: str
    protocol: str
    test_context: bool
    status: Literal['awaiting_sale','awaiting_claim','awaiting_refund','awaiting_return_receipt','ready_to_finalize','inspection_required','review_required','corrected','payment_accepted','closed_paid','closed_unpaid','financial_exception']
    reason: str
    snapshot: dict
    known_invoice_ids: list[str]
    observations: list[dict]
    correction: dict | None
    closure: dict | None
    finalization: dict | None
    exceptions: list[dict]
    financial_return_id: int | None
    sale_document_id: int | None
    manual_policy: dict | None
    manual_policy_hash: str | None
    actions: dict[str,bool]
    blocking_reasons: list[str]

class OrderView(BaseModel):
    order_id: int
    revision: int
    enabled: bool
    paid_profile_id: str | None
    paid_profile_hash: str | None
    invoice_profile_id: str | None
    invoice_profile_hash: str | None
    cases: list[CaseView]
    actions: dict[str,bool]
    blocking_reasons: list[str]
