from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.customer import Customer
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
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
        result = await session.execute(
            select(Product)
            .where(Product.id.in_(sorted(product_ids)))
            .order_by(Product.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
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
            if not states[product.id].is_effectively_visible:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Product {product.id} is hidden from the shop",
                )
            reserved_quantity = getattr(product, "reserved_quantity", 0) or 0
            available_quantity = product.stock_quantity - reserved_quantity
            if available_quantity < item.quantity and not getattr(product, "allow_backorder", False):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Insufficient stock for product {product.id}; backorders are disabled",
                )

        normalized_phone = self.customer_auth_service.normalize_phone(payload.resolved_customer_phone)
        # Guest orders stay unlinked. A matching phone/email is not proof that
        # the caller owns an existing customer account; only authenticated
        # checkout may associate an order with a customer.
        customer = current_customer
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

        allocations: list[tuple[Product, OrderItem, int]] = []
        for item in payload.items:
            product = products[item.product_id]
            product_price = prices[product.id]
            available_quantity = product.stock_quantity - (getattr(product, "reserved_quantity", 0) or 0)
            quantity_from_stock = min(item.quantity, available_quantity)
            quantity_to_order = item.quantity - quantity_from_stock
            subtotal += product_price.base_price * item.quantity
            total += product_price.price * item.quantity
            order_item = OrderItem(
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
                quantity_from_stock=quantity_from_stock,
                quantity_to_order=quantity_to_order,
                quantity_received_for_order=0,
                procurement_status=(
                    ProcurementStatus.to_order if quantity_to_order else ProcurementStatus.not_required
                ),
            )
            order_items.append(order_item)
            allocations.append((product, order_item, quantity_from_stock))

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
        try:
            session.add(order)
            await session.flush()
            for product, order_item, reserved in allocations:
                if not reserved:
                    continue
                product.reserved_quantity = (getattr(product, "reserved_quantity", 0) or 0) + reserved
                session.add(
                    InventoryMovement(
                        product_id=product.id,
                        movement_type=InventoryMovementType.reservation,
                        quantity=reserved,
                        on_hand_delta=0,
                        reserved_delta=reserved,
                        order_id=order.id,
                        order_item_id=order_item.id,
                        reason="Reserved for customer order",
                        reference_key=f"order:{order.id}:item:{order_item.id}:reservation",
                    )
                )
            if current_customer is not None:
                await session.execute(
                    delete(CustomerCartItem).where(
                        CustomerCartItem.customer_id == current_customer.id,
                        CustomerCartItem.product_id.in_(product_ids),
                    )
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(order)
        return order

    async def update_status(
        self,
        session: AsyncSession,
        order_id: int,
        new_status: OrderStatus,
        *,
        admin_user_id: int,
    ) -> Order:
        # Inventory workflows use one global lock order for shared rows:
        # order items, then order, then products. Receipt posting follows the
        # same order so cancellation/completion cannot deadlock with receiving.
        locked_items = list(
            (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.order_id == order_id)
                    .order_by(OrderItem.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        order = (
            await session.execute(
                select(Order)
                .options(selectinload(Order.items))
                .where(Order.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
        if order.status == new_status:
            return order
        if order.status in (OrderStatus.cancelled, OrderStatus.completed):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot change a terminal {order.status.value} order",
            )
        allowed = {
            OrderStatus.pending: {OrderStatus.confirmed, OrderStatus.paid, OrderStatus.completed, OrderStatus.cancelled},
            OrderStatus.confirmed: {OrderStatus.paid, OrderStatus.completed, OrderStatus.cancelled},
            OrderStatus.paid: {OrderStatus.completed, OrderStatus.cancelled},
        }
        if new_status not in allowed[order.status]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Invalid order status transition: {order.status.value} -> {new_status.value}",
            )

        product_ids = sorted({item.product_id for item in locked_items})
        products = {
            product.id: product
            for product in (
                await session.execute(
                    select(Product)
                    .where(Product.id.in_(product_ids))
                    .order_by(Product.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        try:
            if new_status == OrderStatus.cancelled:
                for item in locked_items:
                    reserved = item.quantity_from_stock + item.quantity_received_for_order
                    if not reserved:
                        item.procurement_status = ProcurementStatus.not_required
                        continue
                    product = products[item.product_id]
                    if product.reserved_quantity < reserved:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Reservation mismatch for product {product.id}",
                        )
                    product.reserved_quantity -= reserved
                    item.procurement_status = ProcurementStatus.not_required
                    session.add(
                        InventoryMovement(
                            product_id=product.id,
                            movement_type=InventoryMovementType.reservation_release,
                            quantity=reserved,
                            on_hand_delta=0,
                            reserved_delta=-reserved,
                            order_id=order.id,
                            order_item_id=item.id,
                            reason="Order cancelled",
                            performed_by_user_id=admin_user_id,
                            reference_key=f"order:{order.id}:item:{item.id}:reservation-release",
                        )
                    )
            elif new_status == OrderStatus.completed:
                for item in locked_items:
                    reserved = item.quantity_from_stock + item.quantity_received_for_order
                    if reserved != item.quantity:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Order item {item.id} still requires procurement",
                        )
                    product = products[item.product_id]
                    if product.reserved_quantity < reserved or product.stock_quantity < reserved:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Reservation mismatch for product {product.id}",
                        )
                    product.reserved_quantity -= reserved
                    product.stock_quantity -= reserved
                    session.add(
                        InventoryMovement(
                            product_id=product.id,
                            movement_type=InventoryMovementType.shipment,
                            quantity=reserved,
                            on_hand_delta=-reserved,
                            reserved_delta=-reserved,
                            order_id=order.id,
                            order_item_id=item.id,
                            reason="Customer order completed",
                            performed_by_user_id=admin_user_id,
                            reference_key=f"order:{order.id}:item:{item.id}:shipment",
                        )
                    )
            order.status = new_status
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(order)
        return order
