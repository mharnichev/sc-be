from __future__ import annotations

from decimal import Decimal
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.models.customer import Customer
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus, OrderHandover
from app.models.product import Product
from app.models.shop import CustomerCartItem
from app.schemas.order import OrderCreate, PublicOrderCreate, PublicOrderResponse
from pydantic import ValidationError
from app.services.order_documents import claim_intent, digest, operation_key, make_document
from app.services.catalog_assembly import product_image_urls
from app.services.nova_poshta_directory import NovaPoshtaDirectory
from app.services.customer_auth import CustomerAuthService
from app.services.catalog_visibility import CatalogVisibility
from app.services.shop_promotion import ShopPromotionService, shop_promotion_service


def canonical_checkout_payload(payload: OrderCreate) -> dict:
    canonical = payload.model_dump(mode="json", by_alias=False)
    # Absent legacy preference and explicit false represent the same intent.
    if canonical["callback_requested"] is False:
        canonical.pop("callback_requested")
    canonical["items"] = sorted(canonical["items"], key=lambda item: item["product_id"])
    return canonical


def snapshot_image_url(product: Product) -> str | None:
    # Checkout has loaded media relationships; simple test products may not.
    if not hasattr(product, "attributes_json"):
        return getattr(product, "image_url", None)
    return next(iter(product_image_urls(product)), None)


class OrderService:
    def __init__(self) -> None:
        self.customer_auth_service = CustomerAuthService()

    async def create_order(
        self,
        session: AsyncSession,
        payload: OrderCreate,
        *,
        current_customer: Customer | None = None,
        idempotency_key: str | None = None,
        pos: bool = False,
        simulation: bool = False,
        commit: bool = True,
        raw_public_payload: dict | None = None,
    ) -> Order:
        # Directory validation normalizes labels; never mutate the caller's retry payload.
        payload = payload.model_copy(deep=True)
        intent = None
        if idempotency_key is not None:
            scope = f"checkout:customer:{current_customer.id}" if current_customer else "checkout:guest"
            key = operation_key(idempotency_key, scope)
            intent = await claim_intent(session, key, digest(canonical_checkout_payload(payload)))
            if intent.order_id is not None:
                order = (await session.execute(
                    select(Order).options(selectinload(Order.items)).where(Order.id == intent.order_id)
                )).scalar_one()
                order._checkout_response = intent.response_json
                return order
        if raw_public_payload is not None:
            try:
                payload = PublicOrderCreate.model_validate(raw_public_payload)
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=[
                    {'loc': ['body', *error['loc']], 'msg': error['msg'], 'type': error['type']}
                    for error in exc.errors(include_context=False)
                ]) from None
        if not pos:
            await NovaPoshtaDirectory().validate_order(session, payload)
        product_ids = [item.product_id for item in payload.items]
        if len(product_ids) != len(set(product_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate products are not allowed in order items",
            )
        result = await session.execute(
            select(Product)
            .options(selectinload(Product.images), selectinload(Product.image_variants))
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
        if not pos and any(not states[product_id].is_effectively_visible for product_id in product_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more products are hidden from the shop",
            )

        # Validate purchase availability before resolving promotions.  This
        # keeps an unavailable checkout from doing unnecessary promotion work
        # and guarantees that zero-stock/out-of-stock products are rejected
        # consistently for every item in a batch.
        for item in payload.items:
            if not pos and current_customer is None and item.quantity > 10:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Anonymous customers cannot order more than 10 units of one product",
                )
            product = products[item.product_id]
            if not pos and not states[product.id].is_effectively_visible:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Product {product.id} is hidden from the shop",
                )
            reserved_quantity = getattr(product, "reserved_quantity", 0) or 0
            available_quantity = product.stock_quantity - reserved_quantity
            if available_quantity < item.quantity and (pos or not getattr(product, "allow_backorder", False)):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Insufficient stock for product {product.id}; backorders are disabled",
                )

        normalized_phone = self.customer_auth_service.normalize_phone(payload.resolved_customer_phone) if payload.resolved_customer_phone else None
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
            customer_id=current_customer.id if current_customer else None,
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
                product_image_url=snapshot_image_url(product),
                product_variant=getattr(product, "package_size", None),
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

        fulfillment_method = None
        if payload.shipping_company in ("nova_poshta", "novaPoshta", "novaPost"):
            fulfillment_method = "nova_poshta"
        elif payload.shipping_method in ("pickup", "self_pickup", "selfPickup"):
            fulfillment_method = "pickup"
        order = Order(
            channel="backoffice_pos" if pos else "shop",
            simulation=simulation,
            fulfillment_method=fulfillment_method,
            revision=1,
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
            callback_requested=None if pos else payload.callback_requested,
            external_sync_status="disabled",
            subtotal_amount=ShopPromotionService._money(subtotal),
            discount_amount=ShopPromotionService._money(subtotal - total),
            promo_code=applied_code,
            total_amount=ShopPromotionService._money(total),
            items=order_items,
        )
        try:
            if order.customer_id is None:
                order.guest_tracking_expires_at = datetime.now(UTC) + timedelta(days=7)
            session.add(order)
            await session.flush()
            document = make_document(order)
            session.add(document)
            await session.flush()
            # Populate without triggering asynchronous lazy IO during serialization.
            set_committed_value(order, "preliminary_documents", [document])
            set_committed_value(order, "shipments", [])
            set_committed_value(order, "handovers", [])
            set_committed_value(order, "payments", [])
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
            if intent is not None:
                intent.order_id = order.id
                # Reuse the products already locked for this checkout; presentation needs no extra queries.
                for item in order_items:
                    set_committed_value(item, "product", products[item.product_id])
                response = PublicOrderResponse.model_validate(order).model_dump(mode="json")
                intent.response_json = response
                order._checkout_response = response
            if commit:
                await session.commit()
            else:
                await session.flush()
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
        profile_handover: bool = False,
        commit: bool = True,
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
                    .execution_options(populate_existing=True)
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
        from app.services.qr_payment import block_mutation
        await block_mutation(session, order_id, handover=profile_handover and new_status == OrderStatus.completed)
        if not profile_handover and ((order.channel in ("shop", "backoffice_pos") and new_status in (OrderStatus.paid, OrderStatus.completed))
                or (new_status == OrderStatus.completed and any(s.active for s in order.shipments))):
            raise HTTPException(409, detail={"code": "profile_command_required", "message": "Use payments or handover command; real calculations are disabled in local_only"})
        if new_status == OrderStatus.cancelled and order.channel == "shop" and not order.simulation:
            from app.models.shop_payment_resolutions import ShopPaymentResolutionCase
            if await session.scalar(select(ShopPaymentResolutionCase.id).where(ShopPaymentResolutionCase.order_id==order.id)):
                raise HTTPException(409,"Use the canonical payment resolution finalization")
            from app.models.shop_np_returns import ShopNPReturnCase
            if await session.scalar(select(ShopNPReturnCase.id).where(ShopNPReturnCase.order_id==order.id)):
                raise HTTPException(409,detail={"code":"np_return_workflow_required","message":"Resolve the canonical Nova Poshta return workflow"})
            from app.models.shop_payments import ShopPaymentIntent
            if await session.scalar(select(ShopPaymentIntent.id).where(ShopPaymentIntent.order_id == order.id)):
                raise HTTPException(409, detail={"code": "payment_intent_reconciliation_required", "message": "An issued payment reference requires explicit financial reconciliation before cancellation"})
            from app.models.shop_sales import ShopPaymentConfirmation, ShopFulfillmentFact
            if await session.scalar(select(ShopFulfillmentFact.id).where(ShopFulfillmentFact.order_id == order.id)):
                raise HTTPException(409, detail={"code": "shop_return_required", "message": "A dispatched Shop order requires the return workflow"})
            if await session.scalar(select(ShopPaymentConfirmation.id).where(ShopPaymentConfirmation.order_id == order.id)):
                raise HTTPException(409, detail={"code": "shop_refund_required", "message": "Accepted Shop payment requires refund or void reconciliation"})
        if order.handovers and new_status == OrderStatus.cancelled:
            raise HTTPException(409, detail={"code": "already_handed_over", "message": "Handover cannot be cancelled"})
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
            if new_status in (OrderStatus.cancelled, OrderStatus.completed):
                await self.validate_owned_reservations(session, order, locked_items)
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
                self.apply_handover_inventory(session, order, locked_items, products, admin_user_id)
            if new_status == OrderStatus.completed and not profile_handover:
                session.add(OrderHandover(order_id=order.id, operator_id=admin_user_id,
                    evidence="legacy_status_completion", simulation=False, created_at=datetime.now(UTC)))
            order.revision = (order.revision or 1) + 1
            order.status = new_status
            if commit:
                await session.commit()
            else:
                await session.flush()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(order)
        return order


    @staticmethod
    async def validate_owned_reservations(session, order, locked_items):
        reservation_rows = (await session.execute(
            select(
                InventoryMovement.order_item_id,
                InventoryMovement.product_id,
                func.sum(InventoryMovement.reserved_delta),
            )
            .where(
                InventoryMovement.order_id == order.id,
                InventoryMovement.order_item_id.in_([item.id for item in locked_items]),
            )
            .group_by(InventoryMovement.order_item_id, InventoryMovement.product_id)
        )).all()
        owned_reservations = {
            (item_id, product_id): int(quantity)
            for item_id, product_id, quantity in reservation_rows
        }
        for item in locked_items:
            reserved = item.quantity_from_stock + item.quantity_received_for_order
            if owned_reservations.get((item.id, item.product_id), 0) != reserved:
                # Aggregate stock cannot prove that a historical order
                # owns a newer order's reservation. Reconcile explicitly.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Reservation ownership mismatch for order item {item.id}; reconciliation required",
                )


    @staticmethod
    def apply_handover_inventory(session, order, locked_items, products, admin_user_id):
        """Apply one shipment under items→order→products locks.

        Caller validates owned reservation ledger and terminal state while holding
        the same locks. Both legacy completion and explicit handover use this path.
        Shipment reference keys and order terminal state prevent a second delta.
        """
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
