from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field

from app.models.order import OrderStatus
from app.schemas.common import ORMModel, TimestampedResponse


class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int = Field(ge=1)


class OrderItemResponse(ORMModel):
    id: int
    product_id: int
    quantity: int
    price: Decimal


class OrderCreate(BaseModel):
    customer_name: str = Field(min_length=2, max_length=255)
    customer_phone: str = Field(min_length=5, max_length=50)
    customer_email: EmailStr | None = None
    comment: str | None = None
    items: list[OrderItemCreate] = Field(min_length=1)


class OrderStatusUpdate(BaseModel):
    status: OrderStatus


class OrderResponse(TimestampedResponse):
    id: int
    customer_name: str
    customer_phone: str
    customer_email: EmailStr | None
    comment: str | None
    total_amount: Decimal
    status: OrderStatus
    items: list[OrderItemResponse]


class OrderSummaryResponse(ORMModel):
    id: int
    customer_name: str
    customer_phone: str
    customer_email: EmailStr | None
    total_amount: Decimal
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
