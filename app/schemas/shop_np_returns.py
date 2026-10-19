from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.shop_fiscal import ReturnLine, ReceiptLine
from app.schemas.shop_sales import ShopCommand


class NPCommand(ShopCommand):
    expected_profile_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManualLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1,max_length=100)
    document_id: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,100}$")
    line_id: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,100}$")
    order_id: int = Field(gt=0,strict=True)
    original_shipment_id: int = Field(gt=0,strict=True)
    original_ttn: str = Field(pattern=r"^[0-9]{14}$")
    return_ttn: str = Field(pattern=r"^[0-9]{14}$")
    verified: Literal[True]

    @field_validator("verified",mode="before")
    @classmethod
    def explicit(cls,value):
        if value is not True:
            raise ValueError("Explicit human verification is required")
        return value

    @field_validator("source")
    @classmethod
    def nonblank(cls,value):
        if not value.strip():
            raise ValueError("A nonblank source is required")
        return value.strip()


class AttachCommand(NPCommand):
    original_shipment_id: int = Field(gt=0,strict=True)
    return_ttn: str = Field(pattern=r"^[0-9]{14}$")
    linkage_kind: Literal["easy_return","manual_document"]
    manual_link: ManualLink | None = None
    lines: list[ReturnLine] = Field(min_length=1,max_length=200)
    reason: str = Field(min_length=1,max_length=500)

    @model_validator(mode="after")
    def exact_link(self):
        if (self.linkage_kind=="manual_document") != bool(self.manual_link):
            raise ValueError("Only manual linkage requires separate verified documentary evidence")
        return self


class ReceiveCommand(NPCommand):
    lines: list[ReceiptLine] = Field(min_length=1,max_length=200)
    contents_verified: Literal[True]
    evidence: str = Field(min_length=1,max_length=500)
    _verified=field_validator("contents_verified",mode="before")(ManualLink.explicit.__func__)
    _evidence=field_validator("evidence")(ManualLink.nonblank.__func__)


class CloseCommand(NPCommand):
    confirm: Literal[True]
    evidence: str = Field(min_length=1,max_length=500)
    _confirm=field_validator("confirm",mode="before")(ManualLink.explicit.__func__)
    _evidence=field_validator("evidence")(ManualLink.nonblank.__func__)


class ClaimCommand(NPCommand):
    reason: str = Field(min_length=1,max_length=500)
    _reason=field_validator("reason")(ManualLink.nonblank.__func__)


class ReadinessView(BaseModel):
    enabled: bool
    available: bool
    profile_id: str | None
    profile_hash: str | None
    authority: Literal["none","test","provider"]
    transport: Literal["unavailable","mock_http","https"]
    manual_link_allowed: bool
    blocking_reasons: list[str]


class CaseView(BaseModel):
    id: int
    order_id: int
    revision: int
    order_revision: int
    profile_id: str
    profile_hash: str
    authority: Literal["test","provider"]
    transport: Literal["mock_http","https"]
    test_context: bool
    original_shipment_id: int
    original_ttn: str
    return_ttn: str
    financial_mode: Literal["paid","unpaid_cod"]
    linkage_kind: Literal["easy_return","manual_document"]
    linkage_authority: Literal["test","provider","manual"]
    manual_link: dict | None
    sale_document_id: int | None
    status: str
    lines: list[dict]
    observation: dict | None
    receipt: dict | None
    closure: dict | None
    financial_return_id: int | None
    financial_document_id: int | None
    financial_remaining_lines: list[dict]
    actions: dict[str,bool]
    blocking_reasons: list[str]


class OrderView(BaseModel):
    order_id: int
    revision: int
    enabled: bool
    readiness: ReadinessView
    original_shipment_id: int | None
    original_ttn: str | None
    financial_mode: Literal["paid","unpaid_cod"] | None
    eligible_lines: list[dict]
    cases: list[CaseView]
    actions: dict[str,bool]
    blocking_reasons: list[str]
