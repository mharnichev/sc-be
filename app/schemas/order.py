from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.order import OrderStatus
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
    shipping_area: str | None = Field(default=None, alias="shippingArea", max_length=255)
    shipping_city: str | None = Field(default=None, alias="shippingCity", max_length=255)
    shipping_warehouse_number: str | None = Field(default=None, alias="shippingWarehouseNumber", max_length=100)
    shipping_street: str | None = Field(default=None, alias="shippingStreet", max_length=255)
    building_number: str | None = Field(default=None, alias="buildingNumber", max_length=50)
    shipping_apartment: str | None = Field(default=None, alias="shippingApartment", max_length=50)
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
            "shippingCompany": self.shipping_company,
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


class OrderResponse(TimestampedResponse):
    id: int
    customer_name: str
    customer_phone: str
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
    external_id: str | None = None
    external_sync_status: str | None = None
    external_sync_error: str | None = None
    subtotal_amount: Decimal
    discount_amount: Decimal
    promo_code: str | None = None
    total_amount: Decimal
    status: OrderStatus
    items: list[OrderItemResponse]


class OrderSummaryResponse(ORMModel):
    id: int
    customer_name: str
    customer_phone: str
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
