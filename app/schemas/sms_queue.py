from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SmsQueueJobResponse(BaseModel):
    """Operational data only: excludes message bodies, credentials and OTPs."""
    model_config = ConfigDict(from_attributes=True)
    id: str
    operation: str
    priority: int
    status: str
    attempts: int
    available_at: datetime
    claimed_at: datetime | None
    transport_started_at: datetime | None
    lease_expires_at: datetime | None
    expires_at: datetime | None
    provider_message_id: str | None
    accepted_at: datetime | None
    delivered_at: datetime | None
    delivery_status: str | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime


class SmsQueueConfiguration(BaseModel):
    account_key: str
    provider_requests_per_second: float
    campaign_recipients_per_minute_default: int
    batch_size: int
    concurrency: int
    worker_enabled: bool
    sending_mode: Literal["individual"] = "individual"


class SmsQueueProgress(BaseModel):
    total: int
    counts: dict[str, int]
    dispatching: int = 0
    paused: bool = False
    cancelled: bool = False
    sms_recipients_per_minute: int
    estimated_remaining_seconds: int | None = None
    estimated_completion_at: datetime | None = None
    next_window_at: datetime | None = None
    estimate_kind: Literal["dispatch", "delivery"] = "dispatch"
    estimate_note: str = "Estimate excludes unknown future priority traffic, retries and provider delivery time."


class SmsAccountQueueProgress(BaseModel):
    configuration: SmsQueueConfiguration
    counts: dict[str, int]
    next_request_at: datetime | None = None
    cooldown_until: datetime | None = None


class CancelUnsentResponse(BaseModel):
    run_id: int
    cancelled: int = Field(ge=0)
    status: str
