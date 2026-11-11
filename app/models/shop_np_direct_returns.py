"""Physical facts for the isolated direct Mono Nova Poshta return flow."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, JSON, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ShopNPDirectReturnCase(Base):
    __tablename__ = "shop_np_direct_return_cases"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    original_shipment_id: Mapped[int] = mapped_column(ForeignKey("order_shipments.id", ondelete="RESTRICT"), unique=True)
    fulfillment_id: Mapped[int] = mapped_column(ForeignKey("shop_fulfillment_facts.id", ondelete="RESTRICT"))
    sale_id: Mapped[int] = mapped_column(ForeignKey("shop_mono_checkbox_receipts.id", ondelete="RESTRICT"))
    original_ttn: Mapped[str] = mapped_column(String(14))
    return_ttn: Mapped[str] = mapped_column(String(14), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopNPDirectReturnObservation(Base):
    __tablename__ = "shop_np_direct_return_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("shop_np_direct_return_cases.id", ondelete="RESTRICT"), index=True)
    proof: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShopNPDirectReturnReceipt(Base):
    __tablename__ = "shop_np_direct_return_receipts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("shop_np_direct_return_cases.id", ondelete="RESTRICT"), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("shop_np_direct_return_observations.id", ondelete="RESTRICT"))
    quantities: Mapped[dict[str, int]] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    proof_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
