"""Local POS preparation and explicitly isolated simulations. No provider operations."""
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import OrderIntent, Order, OrderItem, OrderPayment, OrderHandover, OrderStatus
from app.schemas.order import BackofficeOrderResponse, POSOrderCreate, OrderCreate, document_response
from app.services.order import OrderService
from app.services import sales_access
from app.services.order_documents import claim_intent, digest, operation_key, minor


def permitted(actor, *, inventory=False):
    return bool(actor.is_active and (actor.is_superuser or (getattr(actor, "can_record_sales", False) and (not inventory or actor.can_post_inventory))))


def require_permission(actor, *, inventory=False):
    if not permitted(actor, inventory=inventory):
        raise HTTPException(403, detail="Sales and inventory permissions are required" if inventory else "Sales permission is required")


def test_database_url(session):
    url = session.bind.url
    return bool(settings.app_env in ("local", "test")
                and url.get_backend_name() == "postgresql"
                and url.host in ("localhost", "127.0.0.1", "::1")
                and "test" in (url.database or "").lower())


def simulation_available(session):
    return bool(settings.local_sales_simulation_enabled and test_database_url(session))


def live_available(session):
    from app.services.master_mono import guard
    try:
        guard(session, create=True)
    except HTTPException:
        return False
    return True


async def capabilities(session, actor):
    restricted = await sales_access.restricted_master(session, actor)
    allowed = permitted(actor) and (not restricted or actor.can_accept_qr_payments)
    if restricted:
        available = allowed and live_available(session)
        return {"mode": "mono_checkbox", "can_prepare": available, "live_qr_available": available,
                "simulation_available": False,
                "blocking_reasons": [] if available else ["master_mono_disabled"]}
    available = simulation_available(session)
    return {"mode": "local_only", "can_prepare": allowed, "live_qr_available": allowed and live_available(session),
            "simulation_available": available and allowed,
            "blocking_reasons": [] if available and allowed else ["local_only_real_calculation_disabled"]}


async def response(order, session, actor):
    restricted = await sales_access.own_sale(session, actor, order)
    result = BackofficeOrderResponse.model_validate(order)
    result.preliminary_documents = [document_response(d, admin=True) for d in order.preliminary_documents]
    for shipment in result.shipments:
        shipment.label_url = (f"/api/v1/backoffice/orders/{order.id}/shipments/{shipment.id}/label"
            if shipment.tracking_number and shipment.status not in ("unknown", "cancelled") else None)
    mono_owned = bool(order.__dict__.get("master_mono_binding") or order.__dict__.get("master_mono_create_request"))
    enabled = (live_available(session) if mono_owned and not order.simulation else simulation_available(session) and order.simulation) and order.status != OrderStatus.cancelled
    from app.services.shipments import actions
    result.shipment_actions = {} if restricted else actions(order, session, actor)
    from app.services.qr_payment import handover_ready, ACTIVE
    qr_active = any(a.payment_status in ACTIVE for a in order.qr_payment_attempts)
    result.sales_actions = {
        "can_record_payment": not mono_owned and not restricted and enabled and permitted(actor) and not order.payments and not order.handovers and not qr_active,
        "can_handover": enabled and (await master_handover_ready(session, actor, order) if restricted or mono_owned else permitted(actor, inventory=True)) and bool(order.payments) and not order.handovers and order.fulfillment_method != "nova_poshta" and not order.shipments and handover_ready(order),
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
        if payload.simulation:
            raise HTTPException(403, "Masters may only prepare live mono QR sales")
        from app.services.master_mono import guard
        provider = guard(session, create=True)
        if provider.test_only:
            from app.services.shop_sales import require_test_database
            await require_test_database(session, "master_mono_test_unsafe")
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
    actor = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if not actor:
        raise HTTPException(403, "Active sales operator required")
    restricted = await sales_access.restricted_master(session, actor)
    require_permission(actor, inventory=handover and not restricted)
    if restricted and not handover:
        raise HTTPException(403, "Masters cannot record manual payments")
    # A committed operation remains replayable after evidence ages or the bank
    # becomes unavailable. Recheck current grants and owner before returning it.
    snapshot = await load(session, order_id)
    await sales_access.own_sale(session, actor, snapshot)
    op = "handover" if handover else "payment"
    intent_key = operation_key(raw_key, f"{op}:{actor.id}:{order_id}")
    request_hash = digest(payload.model_dump(mode="json"))
    previous = await session.get(OrderIntent, intent_key)
    if previous:
        if previous.payload_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict", "message": "Key belongs to another sales request"})
        if previous.response_json:
            return BackofficeOrderResponse.model_validate(previous.response_json)
    expected_revision = payload.expected_revision
    # Refresh stale evidence before acquiring business locks for inventory handover.
    if handover:
        snapshot = await load(session, order_id)
        if snapshot.__dict__.get("master_mono_binding") and not snapshot.simulation:
            from app.services.master_mono import refresh
            from app.schemas.master_mono import MasterMonoCommand
            observations = snapshot.__dict__.get("master_mono_observations", [])
            if snapshot.revision == expected_revision and (not observations or
                    (datetime.now(UTC) - observations[-1].created_at.replace(tzinfo=UTC)).total_seconds() > 30):
                from uuid import uuid4
                refreshed = await refresh(session, actor, order_id, MasterMonoCommand(expected_revision=expected_revision), str(uuid4()))
                expected_revision = refreshed.revision
    order = await load(session, order_id, lock=True)
    await sales_access.own_sale(session, actor, order)
    mono_live = bool(order.__dict__.get("master_mono_binding") and not order.simulation)
    if order.__dict__.get("master_mono_binding") or order.__dict__.get("master_mono_create_request"):
        if not handover:
            raise HTTPException(409, "External mono checkout owns this sale")
        from app.services.master_mono import validate_handover
        await validate_handover(session, actor, order)
    elif restricted:
        await validate_master_handover(session, actor, order)
    intent = await claim_intent(session, intent_key, request_hash)
    if intent.response_json:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    if not mono_live and (not order.simulation or not simulation_available(session)):
        raise HTTPException(409, detail={"code": "local_only", "message": "Real calculations and handover are disabled"})
    if order.revision != expected_revision:
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
            simulation=not mono_live, created_at=now))
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
    if order.__dict__.get("master_mono_binding"):
        from app.services.master_mono import validate_handover
        return await validate_handover(session, actor, order)
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
