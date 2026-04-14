from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.schemas.order import OrderCreate


class OrderService:
    async def create_order(self, session: AsyncSession, payload: OrderCreate) -> Order:
        product_ids = [item.product_id for item in payload.items]
        if len(product_ids) != len(set(product_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate products are not allowed in order items",
            )
        result = await session.execute(select(Product).where(Product.id.in_(product_ids), Product.is_active.is_(True)))
        products = {product.id: product for product in result.scalars().all()}

        if len(products) != len(product_ids):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more products are invalid")

        total = Decimal("0.00")
        order_items: list[OrderItem] = []

        for item in payload.items:
            product = products[item.product_id]
            if product.stock_quantity < item.quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Insufficient stock for product {product.id}",
                )
            total += product.price * item.quantity
            order_items.append(
                OrderItem(
                    product_id=product.id,
                    quantity=item.quantity,
                    price=product.price,
                )
            )
            product.stock_quantity -= item.quantity

        order = Order(
            customer_name=payload.customer_name,
            customer_phone=payload.customer_phone,
            customer_email=payload.customer_email,
            comment=payload.comment,
            total_amount=total,
            items=order_items,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order
