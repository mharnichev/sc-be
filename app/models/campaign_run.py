"""Campaign execution records. Snapshot content is append-only after evaluation."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin


class CampaignRun(TimestampMixin, Base):
    __tablename__ = "campaign_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="RESTRICT"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    segment_snapshots: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    campaign_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    audience_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
