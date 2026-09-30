from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class MovementType(str, enum.Enum):
    opening_balance = "opening_balance"
    receipt = "receipt"
    reservation = "reservation"
    reservation_release = "reservation_release"
    shipment = "shipment"
    customer_return = "customer_return"
    write_off = "write_off"
    inventory_adjustment = "inventory_adjustment"


class InventoryDocumentStatus(str, enum.Enum):
    draft = "draft"
    posted = "posted"


InventoryMovementType = MovementType
InventoryReceiptStatus = InventoryDocumentStatus
InventoryCountStatus = InventoryDocumentStatus


class InventoryMovement(Base):
    """Append-only stock ledger entry; inventory services must never update rows."""

    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="inventory_movement_quantity_positive"),
        CheckConstraint(
            "on_hand_delta <> 0 OR reserved_delta <> 0",
            name="inventory_movement_has_delta",
        ),
        UniqueConstraint("reference_key", name="uq_inventory_movements_reference_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    movement_type: Mapped[MovementType] = mapped_column(Enum(MovementType, name="inventory_movement_type"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    on_hand_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    order_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id", ondelete="SET NULL"), nullable=True, index=True)
    receipt_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_receipts.id", ondelete="SET NULL"), nullable=True, index=True)
    inventory_count_id: Mapped[int | None] = mapped_column(
        "count_id", ForeignKey("inventory_counts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    performed_by_user_id: Mapped[int | None] = mapped_column(
        "admin_user_id", ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reference_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    product = relationship("Product", back_populates="inventory_movements")
    order = relationship("Order", back_populates="inventory_movements")
    order_item = relationship("OrderItem", back_populates="inventory_movements")
    receipt = relationship("InventoryReceipt", back_populates="movements")
    count = relationship("InventoryCount", back_populates="movements")
    performed_by_user = relationship("AdminUser")


class InventoryReceipt(TimestampMixin, Base):
    __tablename__ = "inventory_receipts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_inventory_receipts_idempotency_key"),
        UniqueConstraint(
            "posted_idempotency_key",
            name="uq_inventory_receipts_posted_idempotency_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[InventoryDocumentStatus] = mapped_column(
        Enum(InventoryDocumentStatus, name="inventory_document_status"),
        default=InventoryDocumentStatus.draft,
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by_user_id: Mapped[int | None] = mapped_column(
        "posted_by_admin_id", ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    posted_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    items = relationship("InventoryReceiptItem", back_populates="receipt", cascade="all, delete-orphan")
    movements = relationship("InventoryMovement", back_populates="receipt")
    posted_by_user = relationship("AdminUser", foreign_keys=[posted_by_user_id])


class InventoryReceiptItem(Base):
    __tablename__ = "inventory_receipt_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="inventory_receipt_item_quantity_positive"),
        CheckConstraint("purchase_cost >= 0", name="inventory_receipt_item_purchase_cost_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("inventory_receipts.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_unit_cost: Mapped[Decimal] = mapped_column("purchase_cost", Numeric(10, 2), nullable=False)
    batch_number: Mapped[str | None] = mapped_column("lot_number", String(100), nullable=True)
    expiration_date: Mapped[date | None] = mapped_column("expires_at", Date, nullable=True)

    receipt = relationship("InventoryReceipt", back_populates="items")
    product = relationship("Product", back_populates="inventory_receipt_items")
    allocations = relationship("InventoryReceiptAllocation", back_populates="receipt_item", cascade="all, delete-orphan")

    @property
    def barcode(self) -> str | None:
        return self.product.barcode if self.product is not None else None


class InventoryReceiptAllocation(Base):
    __tablename__ = "inventory_receipt_allocations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="inventory_receipt_allocation_quantity_positive"),
        UniqueConstraint("receipt_item_id", "order_item_id", name="uq_inventory_receipt_allocations_receipt_item_order_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_receipt_items.id", ondelete="CASCADE"), nullable=False, index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    receipt_item = relationship("InventoryReceiptItem", back_populates="allocations")
    order_item = relationship("OrderItem", back_populates="receipt_allocations")


class InventoryCount(TimestampMixin, Base):
    __tablename__ = "inventory_counts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_inventory_counts_idempotency_key"),
        UniqueConstraint(
            "posted_idempotency_key",
            name="uq_inventory_counts_posted_idempotency_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[InventoryDocumentStatus] = mapped_column(
        Enum(InventoryDocumentStatus, name="inventory_document_status"),
        default=InventoryDocumentStatus.draft,
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by_user_id: Mapped[int | None] = mapped_column(
        "posted_by_admin_id", ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    posted_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    items = relationship("InventoryCountItem", back_populates="count", cascade="all, delete-orphan")
    movements = relationship("InventoryMovement", back_populates="count")
    posted_by_user = relationship("AdminUser", foreign_keys=[posted_by_user_id])


class InventoryCountItem(Base):
    __tablename__ = "inventory_count_items"
    __table_args__ = (
        CheckConstraint("expected_quantity >= 0", name="inventory_count_item_expected_nonnegative"),
        CheckConstraint("counted_quantity >= 0", name="inventory_count_item_counted_nonnegative"),
        UniqueConstraint("count_id", "product_id", name="uq_inventory_count_items_count_product"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_count_id: Mapped[int] = mapped_column(
        "count_id", ForeignKey("inventory_counts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    expected_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    counted_quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    count = relationship("InventoryCount", back_populates="items")
    product = relationship("Product", back_populates="inventory_count_items")

    @property
    def barcode(self) -> str | None:
        return self.product.barcode if self.product is not None else None

    @property
    def difference(self) -> int:
        return self.counted_quantity - self.expected_quantity
