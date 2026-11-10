"""Close an order's lifecycle without reversing financial or fulfillment facts."""
import asyncio
from collections import Counter
from datetime import UTC, datetime
import logging

from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.core import error_reporting
from app.core.logging import request_id_var
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import OrderStatus
from app.models.product import Product
from app.models.shop_sales import ShopOperationAudit
from app.schemas.order import OrderArchiveReconciliation, OrderCancellationState
from app.services import shop_sales as sales
from app.services.order_documents import claim_intent, digest, operation_key
from app.services.shipments import carrier_order

logger = logging.getLogger(__name__)
ARCHIVE = "owner_cancel_order"


def goods_need_review(order):
    return bool(order.handovers or order.shop_fulfillment_fact or any(
        shipment.active or shipment.handed_over_at for shipment in order.shipments))


async def reserve_plan(session, order, *, lock=False):
    """Prove ownership from the ledger and leave doubtful units for review."""
    if order.archived:
        return [], {}, False
    items = {item.id: item for item in order.items}
    rows = (await session.execute(select(InventoryMovement.order_item_id, InventoryMovement.product_id,
        func.sum(InventoryMovement.reserved_delta)).where(InventoryMovement.order_id == order.id)
        .group_by(InventoryMovement.order_item_id, InventoryMovement.product_id))).all()
    units = {}
    anomaly = False
    for item_id, product_id, quantity in rows:
        item = items.get(item_id)
        if item is None or item.product_id != product_id:
            anomaly |= quantity != 0
            continue
        units[item_id] = quantity
        anomaly |= not 0 <= quantity <= item.quantity

    planned = []
    totals = Counter()
    goods_out = goods_need_review(order)
    for item_id, item in sorted(items.items()):
        quantity = units.get(item_id, 0)
        if not goods_out and item_id not in units and item.quantity_from_stock + item.quantity_received_for_order:
            anomaly = True
        if 0 < quantity <= item.quantity:
            planned.append(dict(order_item_id=item_id, product_id=item.product_id, quantity=quantity,
                reference_key=f"order:{order.id}:item:{item_id}:owner-archive-release"))
            totals[item.product_id] += quantity
    if not totals:
        return [], {}, anomaly
    query = select(Product).where(Product.id.in_(totals)).order_by(Product.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    products = {product.id: product for product in (await session.scalars(query)).all()}
    balance = func.sum(InventoryMovement.reserved_delta)
    positive = (select(InventoryMovement.product_id, balance.label("quantity"))
        .where(InventoryMovement.product_id.in_(totals))
        .group_by(InventoryMovement.product_id, InventoryMovement.order_id, InventoryMovement.order_item_id)
        .having(balance > 0).subquery())
    proven_totals = dict((await session.execute(select(positive.c.product_id, func.sum(positive.c.quantity))
        .group_by(positive.c.product_id))).all())
    unsafe = {product_id for product_id, quantity in totals.items()
        if product_id not in products or products[product_id].reserved_quantity < max(quantity, proven_totals[product_id])}
    return [line for line in planned if line["product_id"] not in unsafe], products, anomaly or bool(unsafe)


async def state(session, order, *, plan=None):
    audit = order.owner_archive if order.archived else None
    late = order.archive_late_payment_verified
    lines, _, anomaly = plan if plan is not None else await reserve_plan(session, order)
    if audit:
        facts = audit.facts
        payment_review = facts.get("reconcile_payment", False) or late
        receipt_review = facts.get("reconcile_receipt", False) or late
        inventory_review = facts["inventory_reconciliation"] or goods_need_review(order)
    else:
        paid = bool(order.payments or order.shop_payment_confirmation or order.master_mono_payment)
        pending = bool(order.master_mono_create_request or order.qr_payment_attempts)
        if order.shop_online_payment_request or order.shop_payment_intent:
            pending |= not bool(await session.scalar(select(func.shop_online_dispatch_pending(order.id))))
        settled_return = order.payment_status.endswith("refunded")
        payment_review = not settled_return and (paid or pending)
        receipt_review = not settled_return and paid
        inventory_review = anomaly or goods_need_review(order)
    return OrderCancellationState(order_id=order.id, revision=order.revision, order_status=order.status,
        archived=order.archived, can_cancel=not order.archived, payment_status=order.payment_status,
        fiscal_status=order.fiscal_status, fulfillment_status=order.fulfillment_status,
        cancelled_at=audit.created_at if audit else None, cancelled_by_user_id=audit.operator_id if audit else None,
        comment=audit.facts.get("reason") if audit else None,
        active_reservation_quantity=sum(line["quantity"] for line in lines),
        released_reservation_quantity=sum(line["quantity"] for line in audit.facts["reserve_release"]) if audit else 0,
        late_payment_verified=late, reconciliation=OrderArchiveReconciliation(
            payment=payment_review, receipt=receipt_review, inventory=inventory_review))


async def read(session, actor, order_id):
    order = await sales.load_order(session, order_id)
    await authorize(session, actor, order)
    return await state(session, order)


async def authorize(session, actor, order):
    operator = await sales.current_actor(session, actor, "complete", order)
    if carrier_order(order) and not (operator.is_superuser or operator.can_manage_shipments):
        raise HTTPException(403, "Shipment permission required")
    return operator


def send_incident(order_id, code):
    try:
        error_reporting.report("order_archive", code, order_id=order_id)
    except Exception:
        pass


def incident_in_background(order_id, code):
    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(send_incident, order_id, code))
    except RuntimeError:
        pass


def log_outcome(order_id, outcome, *, anomaly=False):
    correlation = digest(["owner-order-archive", request_id_var.get()])[:24]
    context = request_id_var.set(correlation)
    try:
        logger.log(logging.ERROR if outcome == "error" else logging.INFO,
            "order_archive outcome=%s order_id=%d reserve_review=%s", outcome, order_id, anomaly)
    except Exception:
        pass
    finally:
        request_id_var.reset(context)


async def cancel(session, actor, order_id, payload, raw_key):
    anomaly = False
    applied = False
    try:
        order = await sales.load_order(session, order_id, lock=True)
        operator = await authorize(session, actor, order)
        key = operation_key(raw_key, f"owner-order-archive:{operator.id}:{order.id}")
        intent = await claim_intent(session, key, digest(payload.model_dump(mode="json")))
        if intent.response_json is not None:
            result = OrderCancellationState.model_validate(intent.response_json)
        elif order.archived:
            result = await state(session, order)
        else:
            if payload.expected_revision != order.revision:
                current = await state(session, order)
                raise HTTPException(409, detail={"code": "revision_conflict",
                    "message": "Reload the current order before confirming cancellation",
                    "cancellation": current.model_dump(mode="json")})
            plan = await reserve_plan(session, order, lock=True)
            lines, products, anomaly = plan
            current = await state(session, order, plan=plan)
            session.add(ShopOperationAudit(order_id=order.id, operator_id=operator.id, operation=ARCHIVE,
                request_key=key, created_at=datetime.now(UTC), facts={
                    "expected_revision": order.revision, "reason": payload.comment, "reserve_release": lines,
                    "inventory_reconciliation": anomaly, "reconcile_payment": current.reconciliation.payment,
                    "reconcile_receipt": current.reconciliation.receipt, "reconcile_inventory": current.reconciliation.inventory,
                    "payment_status": current.payment_status,
                    "fulfillment_status": current.fulfillment_status if goods_need_review(order) else "cancelled",
                }))
            await session.flush()
            for line in lines:
                quantity = line["quantity"]
                product = products[line["product_id"]]
                product.reserved_quantity -= quantity
                session.add(InventoryMovement(product_id=product.id,
                    movement_type=InventoryMovementType.reservation_release, quantity=quantity, on_hand_delta=0,
                    reserved_delta=-quantity, order_id=order.id, order_item_id=line["order_item_id"],
                    performed_by_user_id=operator.id, reference_key=line["reference_key"], reason="Owner cancelled order"))
            await session.flush()
            order.status = OrderStatus.cancelled
            order.revision += 1
            await session.flush()
            result = await state(session, await sales.load_order(session, order.id))
            applied = True
        intent.order_id = order_id
        intent.response_json = result.model_dump(mode="json")
        await session.commit()
    except HTTPException as error:
        await session.rollback()
        if error.status_code >= 500:
            log_outcome(order_id, "error")
            incident_in_background(order_id, "unexpected")
        raise
    except Exception:
        await session.rollback()
        log_outcome(order_id, "error")
        incident_in_background(order_id, "unexpected")
        raise
    if applied:
        log_outcome(order_id, "cancelled", anomaly=anomaly)
        if anomaly:
            incident_in_background(order_id, "reserve_integrity")
    return result


async def metrics(session):
    """Database counters and reconciliation age for the existing monitoring stack."""
    row = (await session.execute(text("""
        WITH reconciliation AS (
            SELECT order_id, min(created_at) AS since
            FROM shop_operation_audits
            WHERE operation = 'owner_cancel_late_payment' OR (
                operation = 'owner_cancel_order' AND
                (facts->>'reconcile_payment' = 'true' OR facts->>'reconcile_receipt' = 'true'
                 OR facts->>'inventory_reconciliation' = 'true' OR facts->>'reconcile_inventory' = 'true'))
            GROUP BY order_id
        )
        SELECT
            count(*) FILTER (WHERE operation='owner_cancel_order') AS cancellations_total,
            count(*) FILTER (WHERE operation='owner_cancel_late_payment') AS late_payments_total,
            (SELECT count(*) FROM reconciliation) AS reconciliation_total,
            (SELECT coalesce(extract(epoch FROM now() - min(since))::bigint, 0) FROM reconciliation)
                AS oldest_reconciliation_seconds
        FROM shop_operation_audits WHERE operation IN ('owner_cancel_order', 'owner_cancel_late_payment')
    """))).mappings().one()
    return dict(row)
