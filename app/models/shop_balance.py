from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ShopBalanceEntry(Base):
    __tablename__ = "shop_balance_entries"
    __table_args__ = (
        CheckConstraint("kind IN ('income', 'expense')", name="valid_kind"),
        CheckConstraint("amount_minor BETWEEN 1 AND 999999999999", name="valid_amount"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(7))
    description: Mapped[str] = mapped_column(String(1000))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    author_id: Mapped[int]
    author_email: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )
