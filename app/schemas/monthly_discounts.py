from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


DiscountWarning = Literal[
    "missing_pricing_snapshot",
    "invalid_pricing_snapshot",
    "discount_exceeds_subtotal",
    "snapshot_total_mismatch",
]


class MonthlyDiscountPeriod(BaseModel):
    year: int
    month: int
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    time_basis: Literal["start_at"] = "start_at"


class MonthlyDiscountSummary(BaseModel):
    completed_bookings: int
    discounted_bookings: int
    unknown_discount_bookings: int
    invalid_discount_bookings: int
    known_discount_amount: Decimal


class MonthlyDiscountItem(BaseModel):
    booking_id: int
    start_at: datetime
    completed_at: datetime | None
    customer_id: int | None
    customer_name: str
    customer_phone: str
    master_id: int
    master_name: str | None
    subtotal_amount: Decimal | None
    promotion_discount_amount: Decimal | None
    manual_discount_amount: Decimal | None
    discount_amount: Decimal | None
    total_amount: Decimal | None
    promotion_id: int | None
    promotion_code: str | None
    promotion_name_uk: str | None
    promotion_name_en: str | None
    promotion_discount_percent: int | None
    promotion_application_mode_snapshot: str | None
    promotion_eligibility_type_snapshot: str | None
    campaign_offer_id: int | None
    amount_status: Literal["known", "unknown", "invalid"]
    warnings: list[DiscountWarning]


class MonthlyDiscountsResponse(BaseModel):
    period: MonthlyDiscountPeriod
    scope: Literal["booking_services"] = "booking_services"
    historical_completeness: Literal["not_verifiable"] = "not_verifiable"
    generated_at: datetime
    summary: MonthlyDiscountSummary
    items: list[MonthlyDiscountItem]
    total: int
    offset: int
    limit: int
