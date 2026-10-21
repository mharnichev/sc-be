from __future__ import annotations

import enum
from decimal import Decimal
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
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
    customer_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
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

    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fulfillment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=True)
    simulation: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    guest_tracking_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    guest_tracking_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipments = relationship("Shipment", lazy="selectin", order_by="Shipment.id")

    shop_online_payment_request = relationship("ShopOnlinePaymentRequest", uselist=False, lazy="selectin")

    @property
    def payment_channel(self):
        # Infer origin only from the canonical, verified payment; checkout choice
        # and a generic card fact alone never establish online payment.
        from app.services.shop_sales import canonical_payment
        confirmation = canonical_payment(self)
        if (confirmation and confirmation.source == "provider"
                and confirmation.namespace.startswith("shop_r2_monobank:")
                and confirmation.snapshot.get("original_payment_method") == "onlineCard"):
            return "online_monobank"
        return None

    @property
    def guest_payment_token(self):
        if self.customer_id is not None or not self.guest_tracking_expires_at or self.guest_tracking_revoked_at:
            return None
        from app.core.security import create_scoped_token
        return create_scoped_token(self.id, "guest_order_payment", self.guest_tracking_expires_at)

    @property
    def guest_tracking_url(self):
        if self.customer_id is not None or self.guest_tracking_expires_at is None or self.guest_tracking_revoked_at:
            return None
        from app.core.security import create_scoped_token
        return "/api/v1/public/orders/tracking/" + create_scoped_token(self.id, "guest_order_tracking", self.guest_tracking_expires_at)

    master_mono_create_request = relationship("MasterMonoCreateRequest", uselist=False, lazy="selectin")
    master_mono_binding = relationship("MasterMonoBinding", uselist=False, lazy="selectin")
    master_mono_payment = relationship("MasterMonoPayment", uselist=False, lazy="selectin")
    master_mono_receipt = relationship("MasterMonoReceipt", uselist=False, lazy="selectin")
    master_mono_observations = relationship("MasterMonoObservation", lazy="selectin", order_by="MasterMonoObservation.id")
    qr_payment_attempts = relationship("QRPaymentAttempt", lazy="selectin", order_by="QRPaymentAttempt.id")
    payments = relationship("OrderPayment", lazy="selectin")
    handovers = relationship("OrderHandover", lazy="selectin")
    shop_payment_confirmation = relationship("ShopPaymentConfirmation", uselist=False, lazy="selectin")
    shop_payment_intent = relationship("ShopPaymentIntent", uselist=False, lazy="selectin")
    shop_payment_resolutions = relationship("ShopPaymentResolutionCase", lazy="selectin", order_by="ShopPaymentResolutionCase.id")
    shop_np_returns = relationship("ShopNPReturnCase", lazy="selectin", order_by="ShopNPReturnCase.id")
    shop_mono_checkbox_receipt = relationship("ShopMonoCheckboxReceipt", uselist=False, lazy="selectin")
    shop_mono_checkbox_observations = relationship("ShopMonoCheckboxObservation", lazy="selectin", order_by="ShopMonoCheckboxObservation.id")
    shop_payment_evidence = relationship("ShopPaymentEvidence", uselist=False, lazy="selectin")
    shop_fulfillment_fact = relationship("ShopFulfillmentFact", uselist=False, lazy="selectin")
    shop_completion = relationship("ShopCompletion", uselist=False, lazy="selectin")
    shop_fiscal_documents = relationship("ShopFiscalDocument", lazy="selectin", order_by="ShopFiscalDocument.id")
    shop_financial_returns = relationship("ShopFinancialReturn", lazy="selectin", order_by="ShopFinancialReturn.id")
    shop_carrier_returns = relationship("ShopCarrierReturn", lazy="selectin", order_by="ShopCarrierReturn.id")
    shop_checkbox_binding = relationship("ShopCheckboxBinding", uselist=False, lazy="selectin")
    shop_checkbox_documents = relationship("ShopCheckboxDocument", lazy="selectin", order_by="ShopCheckboxDocument.id")
    shop_checkbox_returns = relationship("ShopCheckboxReturn", lazy="selectin", order_by="ShopCheckboxReturn.id")
    preliminary_documents = relationship("PreliminaryDocument", lazy="selectin", order_by="PreliminaryDocument.version")

    @property
    def shipping_settlement_ref(self) -> str | None:
        return (self.shipping_payload_json or {}).get("shippingSettlementRef")

    @property
    def shipping_city_ref(self) -> str | None:
        return (self.shipping_payload_json or {}).get("shippingCityRef")

    @property
    def shipping_warehouse_ref(self) -> str | None:
        return (self.shipping_payload_json or {}).get("shippingWarehouseRef")

    @property
    def shipping_street_ref(self) -> str | None:
        return (self.shipping_payload_json or {}).get("shippingStreetRef")

    @property
    def payment_status(self) -> str:
        from app.services.shop_sales import payment_projection
        projected = payment_projection(self)
        if projected:
            return projected
        if self.__dict__.get("master_mono_binding") and not self.simulation:
            from app.services.master_mono import canonical_payment, accepted_receipt
            return "paid" if canonical_payment(self) and accepted_receipt(self) else "payment_reconciliation_required" if self.payments else "unpaid"
        if len(self.payments) == 1:
            from app.services.mock_cod import validated_cod_snapshot
            if validated_cod_snapshot(self, self.payments[0]):
                return "simulated_paid"
        if self.simulation and self.payments:
            return "simulated_paid"
        if self.channel and (self.status in (OrderStatus.pending, OrderStatus.confirmed)
                or (not self.payments and any(s.simulation and s.handed_over_at for s in self.shipments))):
            return "unpaid"
        return "legacy_unknown"

    @property
    def fulfillment_status(self) -> str:
        from app.services.shop_sales import fulfillment_projection
        projected = fulfillment_projection(self)
        if projected:
            return projected
        if self.handovers and any(s.simulation and s.handed_over_at for s in self.shipments):
            return "simulated_carrier_handover"
        if not self.simulation and self.handovers and self.__dict__.get("master_mono_binding"):
            return "handed_over"
        if self.simulation and self.handovers:
            return "simulated_handed_over"
        if not self.channel:
            return "legacy_unknown"
        if self.status == OrderStatus.cancelled:
            return "cancelled"
        if self.status == OrderStatus.completed:
            return "legacy_unknown"
        return "reserved" if any(i.quantity_from_stock or i.quantity_received_for_order for i in self.items) else "unfulfilled"

    @property
    def fiscal_status(self) -> str:
        if self.__dict__.get("master_mono_binding"):
            from app.services.master_mono import accepted_receipt
            return ("simulated_" if self.simulation else "") + ("master_mono_receipt_accepted" if accepted_receipt(self) else "master_mono_receipt_pending")
        from app.services.shop_mono_checkbox import projection
        direct = projection(self)
        if direct:
            return direct
        from app.services.shop_checkbox import projection
        checkbox = projection(self)
        if checkbox:
            return checkbox
        from app.services.shop_fiscal import fiscal_projection
        projected = fiscal_projection(self)
        if projected:
            return projected
        return "not_requested" if self.channel else "legacy_unknown"

    @property
    def receipt_delivery_status(self) -> str:
        if self.__dict__.get("master_mono_binding"):
            from app.services.master_mono import accepted_receipt, pdf_bytes
            return ("simulated_" if self.simulation else "") + ("master_mono_pdf_available" if pdf_bytes(accepted_receipt(self)) else "master_mono_pdf_pending")
        from app.services.shop_mono_checkbox import has_direct_facts, accepted_receipt, receipt_file
        if has_direct_facts(self):
            receipt = accepted_receipt(self)
            if receipt:
                check = receipt.proof["checks"][0]
                return "mono_checkbox_external_document_available" if receipt_file(receipt) or check.get("taxUrl") else "mono_checkbox_external_document_unavailable"
            return "mono_checkbox_external_pending_document"
        from app.services.shop_checkbox import projection, truth_valid
        checkbox = projection(self)
        if checkbox:
            test = checkbox.startswith("simulated_")
            prefix = "simulated_checkbox_" if test else "checkbox_"
            valid = not checkbox.endswith("invalid") and any(d.__dict__.get("operation") and truth_valid(d.operation)
                for d in self.__dict__.get("shop_checkbox_documents", []))
            return prefix + ("local_print_available" if valid else "pending_document")
        from app.services.shop_fiscal import fiscal_projection, valid_document_truth
        projected = fiscal_projection(self)
        if projected:
            if projected == "simulated_fiscal_invalid":
                return "simulated_pending_document"
            return "simulated_local_print_available" if any(valid_document_truth(d)
                for d in self.__dict__.get("shop_fiscal_documents", [])) else "simulated_pending_document"
        return "pending_document" if self.channel else "legacy_unknown"

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


    @property
    def product_name_en(self) -> str | None:
        # Optional current-catalog display value; never lazy-load or alter snapshots.
        product = self.__dict__.get("product")
        return product.__dict__.get("name_en") if product is not None else None


class OrderIntent(Base):
    __tablename__ = "order_intents"
    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True)
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PreliminaryDocument(Base):
    __tablename__ = "order_preliminary_documents"
    __table_args__ = (UniqueConstraint("order_id", "version"), UniqueConstraint("operation_key"))
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    number: Mapped[str] = mapped_column(String(64), unique=True)
    operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    guest_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def print_url(self) -> str:
        from app.services.order_documents import document_url
        return document_url(self)


class OrderPayment(Base):
    __tablename__ = "order_payments"
    __table_args__ = (CheckConstraint("amount_minor > 0", name="order_payment_positive"), UniqueConstraint("order_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[str] = mapped_column(String(500))
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    simulation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cod_confirmation = relationship('MockCODConfirmation', uselist=False, lazy='selectin')


class OrderHandover(Base):
    __tablename__ = "order_handovers"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), unique=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="RESTRICT"))
    evidence: Mapped[str] = mapped_column(String(500))
    simulation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
