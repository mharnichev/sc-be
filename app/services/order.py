from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shop import CustomerCartItem
from app.schemas.order import OrderCreate
from app.services.customer_auth import CustomerAuthService
from app.services.catalog_visibility import CatalogVisibility
from app.services.shop_promotion import ShopPromotionService, shop_promotion_service


class OrderService:
    def __init__(self) -> None:
        self.customer_auth_service = CustomerAuthService()

    async def create_order(
        self,
        session: AsyncSession,
        payload: OrderCreate,
        *,
        current_customer: Customer | None = None,
    ) -> Order:
        product_ids = [item.product_id for item in payload.items]
        if len(product_ids) != len(set(product_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate products are not allowed in order items",
            )
        result = await session.execute(select(Product).where(Product.id.in_(product_ids)))
        products = {product.id: product for product in result.scalars().all()}

        if len(products) != len(product_ids):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more products are invalid")

        visibility = await CatalogVisibility.load(session)
        states = visibility.product_states(products.values())
        if any(not states[product_id].is_effectively_visible for product_id in product_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more products are hidden from the shop",
            )

        # Validate purchase availability before resolving promotions.  This
        # keeps an unavailable checkout from doing unnecessary promotion work
        # and guarantees that zero-stock/out-of-stock products are rejected
        # consistently for every item in a batch.
        for item in payload.items:
            if current_customer is None and item.quantity > 10:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Anonymous customers cannot order more than 10 units of one product",
                )
            product = products[item.product_id]
            if not visibility.is_available_for_purchase(product):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Product {product.id} is unavailable for purchase",
                )
            if product.stock_quantity < item.quantity:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Insufficient stock for product {product.id}",
                )

        normalized_phone = self.customer_auth_service.normalize_phone(payload.resolved_customer_phone)
        customer = current_customer
        if customer is None:
            customer = (
                await session.execute(select(Customer).where(Customer.phone == normalized_phone))
            ).scalar_one_or_none()
        prices = await shop_promotion_service.price_products(
            session,
            list(products.values()),
            category_parents=visibility.category_parents(),
            promo_code=payload.promo_code,
            customer_phone=normalized_phone,
            validate_code_usage=bool(payload.promo_code),
            lock_code=bool(payload.promo_code),
        )

        subtotal = Decimal("0.00")
        total = Decimal("0.00")
        order_items: list[OrderItem] = []

        for item in payload.items:
            product = products[item.product_id]
            product_price = prices[product.id]
            subtotal += product_price.base_price * item.quantity
            total += product_price.price * item.quantity
            order_items.append(
                OrderItem(
                    product_id=product.id,
                    quantity=item.quantity,
                    base_price=product_price.base_price,
                    price=product_price.price,
                    discount_amount=product_price.discount_amount * item.quantity,
                    shop_promotion_id=product_price.promotion_id,
                    promotion_name=product_price.promotion_name,
                    promotion_code=product_price.promotion_code,
                    product_name=product.name,
                    product_sku=product.sku,
                    total_price=product_price.price * item.quantity,
                )
            )
            product.stock_quantity -= item.quantity

        applied_code = next((item.promotion_code for item in order_items if item.promotion_code), None)

        order = Order(
            customer_id=customer.id if customer else None,
            customer_name=payload.resolved_customer_name,
            customer_phone=normalized_phone,
            customer_email=payload.resolved_customer_email,
            comment=payload.comment,
            first_name=payload.first_name,
            last_name=payload.last_name,
            shipping_company=payload.shipping_company,
            shipping_method=payload.shipping_method,
            shipping_area=payload.shipping_area,
            shipping_city=payload.shipping_city,
            shipping_warehouse_number=payload.shipping_warehouse_number,
            shipping_street=payload.shipping_street,
            building_number=payload.building_number,
            shipping_apartment=payload.shipping_apartment,
            delivery_address=payload.delivery_address,
            shipping_payload_json=payload.shipping_payload,
            payment_method=payload.payment_method,
            external_sync_status="disabled",
            subtotal_amount=ShopPromotionService._money(subtotal),
            discount_amount=ShopPromotionService._money(subtotal - total),
            promo_code=applied_code,
            total_amount=ShopPromotionService._money(total),
            items=order_items,
        )
        session.add(order)
        if current_customer is not None:
            await session.execute(
                delete(CustomerCartItem).where(
                    CustomerCartItem.customer_id == current_customer.id,
                    CustomerCartItem.product_id.in_(product_ids),
                )
            )
        await session.commit()
        await session.refresh(order)
        return order
