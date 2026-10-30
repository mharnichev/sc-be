from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PrivateCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class PolicyCommand(PrivateCommand):
    expected_revision: int = Field(ge=0)
    rate_bps: int | None = Field(ge=0, le=10000)
    reason: str = Field(min_length=1, max_length=500)


class EntryCommand(PrivateCommand):
    order_id: int = Field(gt=0)
    order_item_id: int = Field(gt=0)
    master_id: int = Field(gt=0)
    expected_order_revision: int = Field(gt=0)
    expected_policy_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class ReviewCommand(PrivateCommand):
    expected_revision: int = Field(gt=0)
    needs_review: bool
    reason: str = Field(min_length=1, max_length=500)


class EntryRevision(PrivateCommand):
    id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)


class SettlementPreview(PrivateCommand):
    master_id: int = Field(gt=0)
    entries: list[EntryRevision] = Field(min_length=1, max_length=100)
    expected_total_minor: int = Field(ge=0, le=9223372036854775807)

    @model_validator(mode='after')
    def distinct(self):
        if len({i.id for i in self.entries}) != len(self.entries):
            raise ValueError('Duplicate entries')
        return self


class SettlementCommand(SettlementPreview):
    paid_at: datetime
    reason: str = Field(min_length=1, max_length=500)
    manual_payment_confirmed: Literal[True]

    @field_validator('manual_payment_confirmed', mode='before')
    @classmethod
    def confirmed(cls, value):
        if value is not True:
            raise ValueError('Explicit manual confirmation required')
        return value

    @field_validator('paid_at', mode='before')
    @classmethod
    def timestamp(cls, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Timezone required')
        return value


class PolicyView(BaseModel):
    revision: int
    rate_bps: int | None
    currency: Literal['UAH'] = 'UAH'


class EntryView(BaseModel):
    id: int
    revision: int
    order_id: int
    order_item_id: int
    master_id: int
    master_name: str
    product_id: int
    product_name: str
    quantity: int
    base_minor: int
    rate_bps: int
    reward_minor: int
    currency: Literal['UAH'] = 'UAH'
    sold_at: datetime
    created_at: datetime
    needs_review: bool
    settlement_id: int | None
    status: Literal['pending', 'needs_review', 'transferred']


class SettlementView(BaseModel):
    id: int
    master_id: int
    master_name: str
    total_minor: int
    currency: Literal['UAH'] = 'UAH'
    paid_at: datetime
    created_at: datetime
    entry_ids: list[int]
    status: Literal['transferred']


class PreviewView(BaseModel):
    master_id: int
    entry_ids: list[int]
    total_minor: int
    currency: Literal['UAH'] = 'UAH'
