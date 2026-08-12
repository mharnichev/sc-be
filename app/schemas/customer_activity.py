from __future__ import annotations

from datetime import date, datetime, time

from pydantic import BaseModel

from app.models.booking import BookingStatus
from app.models.waitlist import WaitlistStatus


class CustomerActivityBooking(BaseModel):
    public_id: str
    master_name: str
    service_names: list[str]
    start_at: datetime
    end_at: datetime
    status: BookingStatus


class CustomerActivityWaitlist(BaseModel):
    public_id: str
    master_name: str | None = None
    service_names: list[str]
    desired_date: date
    preferred_time_from: time | None = None
    preferred_time_to: time | None = None
    status: WaitlistStatus
    expires_at: datetime
    offered_start_at: datetime | None = None
    offered_end_at: datetime | None = None
    offer_expires_at: datetime | None = None


class CustomerActivityResponse(BaseModel):
    bookings: list[CustomerActivityBooking]
    waitlist: list[CustomerActivityWaitlist]


class CustomerActivityBookingCancelResponse(BaseModel):
    public_id: str
    status: BookingStatus
    cancelled_at: datetime


class CustomerActivityWaitlistCancelResponse(BaseModel):
    public_id: str
    status: WaitlistStatus


class CustomerActivityBrowserSessionForgetResponse(BaseModel):
    success: bool = True
