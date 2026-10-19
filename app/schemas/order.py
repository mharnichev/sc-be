from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.order import Order, OrderStatus, PreliminaryDocument, ProcurementStatus
from app.schemas.shipment import PublicShipmentResponse, ShipmentResponse, CustomerShipmentResponse
from app.schemas.common import ORMModel, TimestampedResponse


class OrderItemCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    product_id: int = Field(alias="productId")
    quantity: int = Field(ge=1)


class OrderItemResponse(ORMModel):
    id: int
    product_id: int
    quantity: int
    base_price: Decimal | None = None
    price: Decimal
    discount_amount: Decimal = Decimal("0.00")
    shop_promotion_id: int | None = None
    promotion_name: str | None = None
    promotion_code: str | None = None
    product_name: str | None = None
    product_sku: str | None = None
    total_price: Decimal | None = None


class OrderCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    customer_name: str | None = Field(default=None, min_length=2, max_length=255)
    customer_phone: str | None = Field(default=None, min_length=5, max_length=50)
    customer_email: EmailStr | None = None
    first_name: str | None = Field(default=None, alias="firstName", max_length=100)
    last_name: str | None = Field(default=None, alias="lastName", max_length=100)
    phone_number: str | None = Field(default=None, alias="phoneNumber", min_length=5, max_length=50)
    email: EmailStr | None = None
    shipping_company: str | None = Field(default=None, alias="shippingCompany", max_length=50)
    shipping_method: str | None = Field(default=None, alias="shippingMethod", max_length=50)
    shipping_settlement_ref: str | None = Field(default=None, alias="shippingSettlementRef", min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$")
    shipping_city_ref: str | None = Field(default=None, alias="shippingCityRef", min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$")
    shipping_warehouse_ref: str | None = Field(default=None, alias="shippingWarehouseRef", min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$")
    shipping_street_ref: str | None = Field(default=None, alias="shippingStreetRef", min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$")
    shipping_area: str | None = Field(default=None, alias="shippingArea", max_length=255)
    shipping_city: str | None = Field(default=None, alias="shippingCity", max_length=255)
    shipping_warehouse_number: str | None = Field(default=None, alias="shippingWarehouseNumber", max_length=100)
    shipping_street: str | None = Field(default=None, alias="shippingStreet", max_length=255)
    building_number: str | None = Field(default=None, alias="buildingNumber", max_length=50)
    shipping_apartment: str | None = Field(default=None, alias="shippingApartment", max_length=50)
    shipping_payload_json: dict[str, str | None] | None = Field(default=None, alias="shippingPayloadJson")
    payment_method: str | None = Field(default=None, alias="paymentMethod", max_length=50)
    promo_code: str | None = Field(default=None, alias="promoCode", min_length=3, max_length=50)
    comment: str | None = None
    items: list[OrderItemCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_contact(self) -> "OrderCreate":
        if not self.resolved_customer_name:
            raise ValueError("customer_name or firstName/lastName is required")
        if not self.resolved_customer_phone:
            raise ValueError("customer_phone or phoneNumber is required")
        return self

    @property
    def resolved_customer_name(self) -> str:
        if self.customer_name:
            return self.customer_name.strip()
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()

    @property
    def resolved_customer_phone(self) -> str:
        return (self.customer_phone or self.phone_number or "").strip()

    @property
    def resolved_customer_email(self) -> EmailStr | None:
        return self.customer_email or self.email

    @property
    def delivery_address(self) -> str | None:
        parts = [
            self.shipping_area,
            self.shipping_city,
            self.shipping_warehouse_number,
            self.shipping_street,
            self.building_number,
            self.shipping_apartment,
        ]
        address = ", ".join(part.strip() for part in parts if part and part.strip())
        return address or None

    @property
    def shipping_payload(self) -> dict[str, str | None]:
        return {
            **{key: value for key, value in (self.shipping_payload_json or {}).items() if key in {"settlementLabel", "cityLabel", "warehouseLabel", "streetLabel"}},
            "shippingCompany": self.shipping_company,
            "shippingSettlementRef": self.shipping_settlement_ref,
            "shippingCityRef": self.shipping_city_ref,
            "shippingWarehouseRef": self.shipping_warehouse_ref,
            "shippingStreetRef": self.shipping_street_ref,
            "shippingMethod": self.shipping_method,
            "shippingArea": self.shipping_area,
            "shippingCity": self.shipping_city,
            "shippingWarehouseNumber": self.shipping_warehouse_number,
            "shippingStreet": self.shipping_street,
            "buildingNumber": self.building_number,
            "shippingApartment": self.shipping_apartment,
        }


class OrderStatusUpdate(BaseModel):
    status: OrderStatus


class PreliminaryDocumentResponse(ORMModel):
    id: int
    number: str
    version: int
    document_kind: str = "preliminary"
    created_at: datetime
    print_url: str


class OrderProjections(ORMModel):
    shipments: list[PublicShipmentResponse] = Field(default_factory=list)
    shipping_settlement_ref: str | None = None
    shipping_city_ref: str | None = None
    shipping_warehouse_ref: str | None = None
    shipping_street_ref: str | None = None
    simulation: bool = False
    channel: str | None = None
    fulfillment_method: str | None = None
    revision: int = 1
    payment_status: str = "legacy_unknown"
    fulfillment_status: str = "legacy_unknown"
    fiscal_status: str = "legacy_unknown"
    receipt_delivery_status: str = "legacy_unknown"
    preliminary_documents: list[PreliminaryDocumentResponse] = Field(default_factory=list)


class PreliminaryDocumentCreate(BaseModel):
    expected_revision: int = Field(ge=1)


class PublicOrderResponse(TimestampedResponse, OrderProjections):
    guest_tracking_url: str | None = None
    id: int
    customer_name: str
    customer_phone: str | None
    customer_email: EmailStr | None
    comment: str | None
    first_name: str | None = None
    last_name: str | None = None
    shipping_company: str | None = None
    shipping_method: str | None = None
    shipping_area: str | None = None
    shipping_city: str | None = None
    shipping_warehouse_number: str | None = None
    shipping_street: str | None = None
    building_number: str | None = None
    shipping_apartment: str | None = None
    delivery_address: str | None = None
    shipping_payload_json: dict | None = None
    payment_method: str | None = None
    tracking_number: str | None = None
    subtotal_amount: Decimal
    discount_amount: Decimal
    promo_code: str | None = None
    total_amount: Decimal
    status: OrderStatus
    items: list[OrderItemResponse]


class OrderResponse(PublicOrderResponse):
    external_id: str | None = None
    external_sync_status: str | None = None
    external_sync_error: str | None = None


class BackofficeOrderItemResponse(OrderItemResponse):
    quantity_from_stock: int
    quantity_to_order: int
    quantity_received_for_order: int
    procurement_status: ProcurementStatus


class PaymentFactResponse(ORMModel):
    currency: Literal["UAH"] = "UAH"
    id: int
    amount_minor: int
    method: str
    source: str
    evidence: str
    operator_id: int
    simulation: bool
    created_at: datetime


class HandoverFactResponse(ORMModel):
    id: int
    evidence: str
    operator_id: int
    simulation: bool
    created_at: datetime


class BackofficeOrderResponse(OrderResponse):
    shipments: list[ShipmentResponse] = Field(default_factory=list)
    shipment_actions: dict = Field(default_factory=dict)
    created_by_user_id: int | None = None
    items: list[BackofficeOrderItemResponse]
    payments: list[PaymentFactResponse] = Field(default_factory=list)
    handovers: list[HandoverFactResponse] = Field(default_factory=list)
    sales_actions: dict = Field(default_factory=dict)


class OrderFulfillmentResponse(ORMModel):
    id: int
    status: OrderStatus
    items: list[BackofficeOrderItemResponse]


class OrderSummaryResponse(OrderProjections):
    id: int
    customer_name: str
    customer_phone: str | None
    customer_email: EmailStr | None
    subtotal_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    status: OrderStatus
    shipping_company: str | None = None
    shipping_city: str | None = None
    payment_method: str | None = None
    created_at: datetime
    updated_at: datetime


class CustomerOrderItemResponse(ORMModel):
    id: int
    product_id: int
    quantity: int
    price: Decimal
    discount_amount: Decimal
    product_name: str | None = None
    product_sku: str | None = None
    # Current catalog photo; historical name/price/quantity remain order snapshots.
    product_image_url: str | None = None
    total_price: Decimal | None = None


class CustomerOrderResponse(OrderProjections):
    shipments: list["CustomerShipmentResponse"] = Field(default_factory=list)
    delivery_summary: dict[str, int] = Field(default_factory=dict)
    id: int
    created_at: datetime
    status: OrderStatus
    subtotal_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    item_count: int
    items: list[CustomerOrderItemResponse]
    shipping_company: str | None = None
    shipping_method: str | None = None
    shipping_area: str | None = None
    shipping_city: str | None = None
    shipping_warehouse_number: str | None = None
    shipping_street: str | None = None
    building_number: str | None = None
    shipping_apartment: str | None = None
    delivery_address: str | None = None
    payment_method: str | None = None
    tracking_number: str | None = None

    @classmethod
    def from_order(cls, order: Order, *, detail: bool = False) -> "CustomerOrderResponse":
        from app.services.customer_tracking import project
        from app.services.catalog_assembly import loaded_relationship, product_image_urls
        items = []
        for item in order.items:
            image_urls = []
            if loaded_relationship(item, "product") and item.product is not None:
                image_urls = product_image_urls(item.product)
            items.append(CustomerOrderItemResponse.model_validate(item).model_copy(
                update={"product_image_url": image_urls[0] if image_urls else None},
            ))
        shipments = [project(s, detail=detail) for s in getattr(order, 'shipments', [])]
        summary = {}
        for shipment in shipments:
            summary[shipment.delivery_status] = summary.get(shipment.delivery_status, 0) + 1
        return cls.model_validate({
            "shipments": shipments,
            "delivery_summary": summary,
            **{name: getattr(order, name, cls.model_fields[name].default) for name in cls.model_fields if name not in {"items", "item_count", "preliminary_documents", "shipments", "delivery_summary"}},
            "preliminary_documents": [document_response(d, owner=True) for d in getattr(order, "preliminary_documents", [])],
            "items": items,
            "item_count": sum(item.quantity for item in order.items),
        })


def document_response(
    document: PreliminaryDocument, *, owner: bool = False, admin: bool = False,
) -> PreliminaryDocumentResponse:
    from app.services.order_documents import document_url
    return PreliminaryDocumentResponse(
        id=document.id, number=document.number, version=document.version,
        created_at=document.created_at, print_url=document_url(document, owner=owner, admin=admin),
    )


class POSOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[OrderItemCreate] = Field(min_length=1, max_length=100)
    customer_name: str | None = Field(default=None, min_length=2, max_length=255)
    customer_phone: str | None = Field(default=None, min_length=5, max_length=50)
    promo_code: str | None = Field(default=None, min_length=3, max_length=50)
    fulfillment_method: Literal["pickup"] = "pickup"
    simulation: bool = False


class OrderPaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    amount_minor: int = Field(gt=0, strict=True)
    method: Literal["cash", "card"]
    source: Literal["operator_confirmation"]
    evidence: str = Field(min_length=1, max_length=500)


class OrderHandoverCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    evidence: str = Field(min_length=1, max_length=500)
