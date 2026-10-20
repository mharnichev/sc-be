"""Immutable one-shot Shop invoice dispatch ownership, including unknown outcomes."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class ShopOnlinePaymentRequest(Base):
    __tablename__ = "shop_online_payment_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("shop_payment_intents.id", ondelete="RESTRICT"), unique=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True)
    initiator: Mapped[str] = mapped_column(String(16))
    reference: Mapped[str] = mapped_column(String(36), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    profile_id: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSON)
    authority: Mapped[str] = mapped_column(String(16))
    transport: Mapped[str] = mapped_column(String(16))
    basket: Mapped[list] = mapped_column(JSON)
    basket_hash: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
