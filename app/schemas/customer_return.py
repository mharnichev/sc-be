from typing import Literal

from pydantic import BaseModel


class CustomerDirectReturn(BaseModel):
    simulation: Literal[True] = True
    inspection_status: Literal["awaiting_parcel", "awaiting_inspection", "awaiting_confirmation", "received"]
    received_quantity: int | None = None
    tracking_stale: bool = False
    refund_kind: Literal["full", "partial"] | None = None
    money_status: Literal["not_started", "pending", "confirmed"] = "not_started"
    refunded_amount_minor: int | None = None
    currency: Literal["UAH"] | None = None
    return_receipt_status: Literal["not_started", "pending", "confirmed"] = "not_started"
    financial_stale: bool = False
