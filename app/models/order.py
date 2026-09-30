from __future__ import annotations

import enum
from decimal import Decimal

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class OrderStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    paid = "paid"
    completed = "completed"
    cancelled = "cancelled"


class ProcurementStatus(str, enum.Enum):
    not_required = "not_required"
    to_order = "to_order"
    ordered = "ordered"
    received = "received"


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(255))
    customer_phone: Mapped[str] = mapped_column(String(50))
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    shipping_company: Mapped[str | None] = mapped_column(String(50), nullable=True)
    shipping_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    shipping_area: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shipping_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shipping_warehouse_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    shipping_street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    building_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    shipping_apartment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    shipping_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    external_sync_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtotal_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    promo_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.pending, nullable=False)

    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    inventory_movements = relationship("InventoryMovement", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity_from_stock >= 0", name="order_item_quantity_from_stock_nonnegative"),
        CheckConstraint("quantity_to_order >= 0", name="order_item_quantity_to_order_nonnegative"),
        CheckConstraint("quantity_received_for_order >= 0", name="order_item_quantity_received_nonnegative"),
        CheckConstraint(
            "quantity_from_stock + quantity_to_order = quantity",
            name="order_item_fulfillment_quantity_matches_ordered",
        ),
        CheckConstraint(
            "quantity_received_for_order <= quantity_to_order",
            name="order_item_received_not_more_than_shortage",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer)
    quantity_from_stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_to_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_received_for_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    procurement_status: Mapped[ProcurementStatus] = mapped_column(
        Enum(ProcurementStatus, name="procurement_status"),
        default=ProcurementStatus.not_required,
        nullable=False,
    )
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    shop_promotion_id: Mapped[int | None] = mapped_column(
        ForeignKey("shop_promotions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    promotion_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    promotion_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    total_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")
    shop_promotion = relationship("ShopPromotion")
    inventory_movements = relationship("InventoryMovement", back_populates="order_item")
    receipt_allocations = relationship("InventoryReceiptAllocation", back_populates="order_item")
