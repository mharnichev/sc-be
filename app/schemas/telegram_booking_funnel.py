from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel

TELEGRAM_FUNNEL_STEPS = ("booking_start", "contact_entered", "master_selected", "service_selected", "date_selected", "slot_selected", "confirmation_viewed", "booking_success")
TELEGRAM_CONTEXT_STEPS = ("no_slot", "stale_schedule", "booking_error", "draft_cancelled")


class TelegramFunnelStep(BaseModel):
    step: str
    count: int


class TelegramFunnelTransition(BaseModel):
    from_step: str
    to_step: str
    conversion_percent: Decimal | None
    drop_off_count: int
    drop_off_percent: Decimal | None


class TelegramBookingFunnelAggregate(BaseModel):
    date_from: date
    date_to: date
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    cohort_definition: str = "Attempts started during the inclusive Kyiv date range; later ordered milestones are included through query time."
    confirmation_definition: str = "confirmation_viewed means the bot generated the review card, not a Telegram read receipt."
    steps: list[TelegramFunnelStep]
    transitions: list[TelegramFunnelTransition]
    started: int
    succeeded: int
    conversion_percent: Decimal | None
    success_definition: str = "succeeded counts ordered full milestone chains; created_bookings counts valid persisted Telegram bookings in the started cohort even if an earlier milestone is missing."
    created_bookings: int
    tracking_gap_successes: int
    completed_visits: int
    contextual_steps: list[TelegramFunnelStep]
