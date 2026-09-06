from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin


class SegmentStatus(str, enum.Enum):
    active = "active"
    archived = "archived"


class CustomerSegment(TimestampMixin, Base):
    __tablename__ = "customer_segments"
    __table_args__ = (CheckConstraint("revision >= 1", name="revision_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SegmentStatus] = mapped_column(
        Enum(SegmentStatus, native_enum=False), default=SegmentStatus.active, nullable=False, index=True
    )
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
