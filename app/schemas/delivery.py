from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel


class SettlementOption(BaseModel):
    ref: str
    city_ref: str | None
    name: str
    label: str
    area: str
    region: str
    delivery_available: bool


class BranchOption(BaseModel):
    ref: str
    settlement_ref: str
    city_ref: str | None
    number: str
    name: str
    label: str
    address: str
    type: Literal["warehouse", "postomat"]
    status: str


class StreetOption(BaseModel):
    ref: str
    name: str
    label: str


Option = TypeVar("Option")


class DirectoryResponse(BaseModel, Generic[Option]):
    items: list[Option]
    cached: bool
    updated_at: datetime | None = None
    stale: bool = False
