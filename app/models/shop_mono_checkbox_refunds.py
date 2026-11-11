"""One pre-handover refund dispatch claim and its independent evidence."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ShopMonoCheckboxRefund(Base):
    __tablename__ = "shop_mono_checkbox_refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("shop_mono_checkbox_receipts.id", ondelete="RESTRICT"), unique=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    ext_ref: Mapped[str] = mapped_column(String(32), unique=True)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    bank_proof: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    receipt_proof: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    return_receipt_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
