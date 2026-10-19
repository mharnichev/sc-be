"""Local POS preparation and explicitly isolated simulations. No provider operations."""
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.order import Order, OrderItem, OrderPayment, OrderHandover, OrderStatus
from app.schemas.order import BackofficeOrderResponse, POSOrderCreate, OrderCreate, document_response
from app.services.order import OrderService
from app.services import sales_access
from app.services.order_documents import claim_intent, digest, operation_key, minor


def permitted(actor, *, inventory=False):
    return bool(actor.is_active and (actor.is_superuser or (getattr(actor, "can_record_sales", False) and (not inventory or actor.can_post_inventory))))


def require_permission(actor, *, inventory=False):
    if not permitted(actor, inventory=inventory):
        raise HTTPException(403, detail="Sales and inventory permissions are required" if inventory else "Sales permission is required")


def simulation_available(session):
    url = session.bind.url
    return bool(settings.local_sales_simulation_enabled and settings.app_env in ("local", "test")
                and url.get_backend_name() == "postgresql"
                and url.host in ("localhost", "127.0.0.1", "::1")
                and "test" in (url.database or "").lower())


async def capabilities(session, actor):
    restricted = await sales_access.restricted_master(session, actor)
    allowed = permitted(actor) and (not restricted or actor.can_accept_qr_payments)
    available = simulation_available(session)
    return {"mode": "local_only", "can_prepare": allowed,
            "simulation_available": available and allowed,
            "blocking_reasons": [] if available and allowed else ["local_only_real_calculation_disabled"]}


async def response(order, session, actor):
    restricted = await sales_access.own_sale(session, actor, order)
    result = BackofficeOrderResponse.model_validate(order)
    result.preliminary_documents = [document_response(d, admin=True) for d in order.preliminary_documents]
    for shipment in result.shipments:
        shipment.label_url = (f"/api/v1/backoffice/orders/{order.id}/shipments/{shipment.id}/label"
            if shipment.tracking_number and shipment.status not in ("unknown", "cancelled") else None)
    enabled = simulation_available(session) and order.simulation and order.status != OrderStatus.cancelled
    from app.services.shipments import actions
    result.shipment_actions = {} if restricted else actions(order, session, actor)
    from app.services.qr_payment import handover_ready, ACTIVE
    qr_active = any(a.payment_status in ACTIVE for a in order.qr_payment_attempts)
    result.sales_actions = {
        "can_record_payment": not restricted and enabled and permitted(actor) and not order.payments and not order.handovers and not qr_active,
        "can_handover": enabled and (await master_handover_ready(session, actor, order) if restricted else permitted(actor, inventory=True)) and bool(order.payments) and not order.handovers and order.fulfillment_method != "nova_poshta" and not order.shipments and handover_ready(order),
        "blocking_reasons": [] if enabled else ["local_only_real_calculation_disabled"],
    }
    return result


async def load(session, order_id, *, lock=False):
    if lock:
        await session.execute(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id).with_for_update())
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


async def prepare(session, actor, payload: POSOrderCreate, raw_key):
    require_permission(actor)
    if await sales_access.restricted_master(session, actor):
        if not actor.can_accept_qr_payments:
            raise HTTPException(403, "Sales and QR payment permissions required")
        if not payload.simulation:
            raise HTTPException(403, "Masters may only prepare local simulated QR sales")
    intent = await claim_intent(session, operation_key(raw_key, f"pos:{actor.id}"), digest(payload.model_dump(mode="json")))
    if intent.response_json:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    if payload.simulation and not simulation_available(session):
        raise HTTPException(409, detail={"code": "simulation_disabled", "message": "Isolated local test database and server permission required"})
    # POS has its own validation contract: no customer identity or delivery required.
    checkout = OrderCreate.model_construct(customer_name=payload.customer_name or "Анонімний покупець",
        customer_phone=payload.customer_phone, promo_code=payload.promo_code, items=payload.items,
        shipping_method="pickup")
    order = await OrderService().create_order(session, checkout, pos=True, simulation=payload.simulation, commit=False)
    if order.total_amount <= 0:
        await session.rollback()
        raise HTTPException(409, detail={"code": "unsupported_zero_total_pos",
            "message": "POS orders require a positive total; adjust the order or promotion before preparation"})
    order.created_by_user_id = actor.id
    await session.flush()
    order = await load(session, order.id)
    result = await response(order, session, actor)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode="json")
    await session.commit()
    return result


async def command(session, actor, order_id, payload, raw_key, *, handover=False):
    restricted = await sales_access.restricted_master(session, actor)
    require_permission(actor, inventory=handover and not restricted)
    if restricted and not handover:
        raise HTTPException(403, "Masters cannot record manual payments")
    order = await load(session, order_id, lock=True)
    await sales_access.own_sale(session, actor, order)
    if restricted:
        await validate_master_handover(session, actor, order)
    op = "handover" if handover else "payment"
    intent = await claim_intent(session, operation_key(raw_key, f"{op}:{actor.id}:{order_id}"), digest(payload.model_dump(mode="json")))
    if intent.response_json:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    if not order.simulation or not simulation_available(session):
        raise HTTPException(409, detail={"code": "local_only", "message": "Real calculations and handover are disabled"})
    if order.revision != payload.expected_revision:
        raise HTTPException(409, detail={"code": "revision_conflict", "message": "Reload order"})
    if order.status in (OrderStatus.cancelled, OrderStatus.completed) or order.handovers:
        raise HTTPException(409, detail={"code": "terminal_order", "message": "Order already closed"})
    from app.services.qr_payment import block_mutation
    await block_mutation(session, order_id, handover=handover)
    now = datetime.now(UTC)
    if handover:
        if order.fulfillment_method == "nova_poshta" or order.shipments:
            raise HTTPException(409, detail={"code": "carrier_handover_required", "message": "Use the shipment carrier handover command"})
        if not order.payments:
            raise HTTPException(409, detail={"code": "payment_required", "message": "Simulated payment required"})
        await OrderService().update_status(session, order_id, OrderStatus.completed,
            admin_user_id=actor.id, profile_handover=True, commit=False)
        session.add(OrderHandover(order_id=order_id, operator_id=actor.id, evidence=payload.evidence,
            simulation=True, created_at=now))
    else:
        if order.payments:
            raise HTTPException(409, detail={"code": "payment_exists", "message": "Payment already recorded"})
        if payload.amount_minor != minor(order.total_amount):
            raise HTTPException(409, detail={"code": "amount_mismatch", "message": "Payment must equal server order total in kopecks"})
        session.add(OrderPayment(order_id=order_id, amount_minor=payload.amount_minor, method=payload.method,
            source=payload.source, evidence=payload.evidence, operator_id=actor.id, simulation=True, created_at=now))
        order.payment_method = payload.method
        order.revision += 1
    await session.flush()
    order = await load(session, order_id)
    result = await response(order, session, actor)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode="json")
    await session.commit()
    return result


async def validate_master_handover(session, actor, order):
    """The narrow handover grant needs canonical provider payment and receipt."""
    from app.models.fiscal import FiscalDocument
    from app.services import fiscal
    from app.services.qr_payment import ACTIVE
    active = [attempt for attempt in order.qr_payment_attempts if attempt.payment_status in ACTIVE]
    if (len(active) != 1 or active[0].actor_id != actor.id
            or active[0].payment_status != "paid" or active[0].receipt_status != "issued"
            or active[0].amount_minor != minor(order.total_amount) or len(order.payments) != 1):
        raise HTTPException(409, "An issued receipt for the exact QR payment is required")
    document = await session.scalar(select(FiscalDocument).where(FiscalDocument.qr_attempt_id == active[0].id))
    if not document or document.payment_id != order.payments[0].id:
        raise HTTPException(409, "An issued receipt for the exact QR payment is required")
    await fiscal.validate_issued_document(session, document)


async def master_handover_ready(session, actor, order):
    try:
        await validate_master_handover(session, actor, order)
    except HTTPException:
        return False
    return True
