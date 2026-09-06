from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.schemas.common import TimestampedResponse
from app.schemas.messaging import MessageRecipientResponse


class CampaignRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=128)
    scheduled_at: AwareDatetime | None = None


class CampaignRunResponse(TimestampedResponse):
    id: int
    campaign_id: int
    idempotency_key: str
    status: str
    scheduled_at: datetime | None
    evaluated_at: datetime | None
    segment_snapshots: list[dict[str, Any]]
    campaign_snapshot: dict[str, Any]
    audience_count: int


class CampaignRunDetail(CampaignRunResponse):
    delivery_counts: dict[str, int] = Field(default_factory=dict)


class CampaignRunMemberResponse(MessageRecipientResponse):
    run_id: int
    snapshot_facts: dict[str, Any] | None = None
    send_started_at: datetime | None = None


class CampaignAudiencePreviewMember(BaseModel):
    customer_id: int
    name: str | None = None
    eligible: bool
    exclusion_reason: str | None = None
    channel: str | None = None
    reachability: dict[str, bool] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)


class CampaignAudiencePreviewResponse(BaseModel):
    evaluated_at: datetime
    total: int
    page: int
    page_size: int
    items: list[CampaignAudiencePreviewMember]
