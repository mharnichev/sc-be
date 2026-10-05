from datetime import datetime
from pydantic import BaseModel, Field


class CampaignOfferAnalytics(BaseModel):
    date_from: datetime | None = None
    date_to: datetime | None = None
    period_basis: str = "run_cohort"
    campaign_id: int | None = None
    run_id: int | None = None
    master_id: int | None = None
    promotion_id: int | None = None
    audience_size: int
    communication_eligible_recipients: int
    provider_accepted: int
    delivered: int
    raw_link_requests: int
    confirmed_page_opens: int
    unique_confirmed_openers: int
    bookings_created: int
    cancelled_bookings: int
    no_show: int
    completed_visits: int
    unique_customers_redeemed: int
    total_discount_amount: int
    observed_completed_revenue: int
    booking_conversion_percent: float | None = Field(description="Bookings created / communication-eligible recipients × 100; null for zero denominator")
    redemption_conversion_percent: float | None = Field(description="Unique redeemed customers / communication-eligible recipients × 100; null for zero denominator")
