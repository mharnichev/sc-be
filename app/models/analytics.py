from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AnalyticsTrackingMarker(Base):
    """Records the first instant a persisted analytics signal was observed."""

    __tablename__ = "analytics_tracking_markers"

    metric_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
