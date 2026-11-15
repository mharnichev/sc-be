from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MasterMonoRefund(Base):
    __tablename__ = "master_mono_refunds"

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    request_key: Mapped[str] = mapped_column(String(64))
    ext_ref: Mapped[str | None] = mapped_column(String(32), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bank_proof: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    receipt_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    receipt_url: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
