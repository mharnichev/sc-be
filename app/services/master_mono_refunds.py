"""Full POS refunds dispatched by Mono and reconciled against its bank and fiscal facts."""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.models.inventory import InventoryMovement, MovementType
from app.models.master_mono_refund import MasterMonoRefund
from app.models.order import OrderStatus
from app.models.product import Product
from app.schemas.master_mono import MasterMonoRefundState
from app.services import master_mono, shop_mono_checkbox
from app.services.monobank_provider import MonobankError
from app.services.order_documents import digest, minor, operation_key
from app.services.shop_mono_checkbox_refunds import bank_evidence, clean_paid_invoice, invoice_matches


def fail(code, status=409):
    raise HTTPException(status, detail={"code": code})


async def context(session, actor, order_id, *, lock=False):
    current, order = await master_mono.actor_order(session, actor, order_id, lock=lock, manager=True)
    if not current.is_superuser and not current.can_fiscalize_returns:
        fail("pos_refund_permission_denied", 403)
    if order.simulation:
        fail("pos_refund_not_found", 404)
    return current, order, await session.get(MasterMonoRefund, order_id, populate_existing=True)


def original(order):
    payment = master_mono.canonical_payment(order)
    sale = order.master_mono_receipt
    binding = order.master_mono_binding
    request = order.master_mono_create_request
    if not payment or not sale or not request:
        return None
    status, check = shop_mono_checkbox.check_proof(sale.proof, binding, binding.invoice_id)
    if (status != "accepted" or sale.proof_hash != digest(sale.proof) or check["id"] != sale.receipt_id
            or sale.payment_id != payment.id or sale.binding_id != binding.id
            or request.id != binding.create_request_id or request.profile_hash != binding.profile_hash
            or request.reference != binding.reference or request.amount_minor != binding.amount_minor
            or request.basket_hash != digest(request.basket)):
        return None
    return {
        "invoice_id": binding.invoice_id, "reference": binding.reference,
        "invoice_amount_minor": payment.amount_minor, "amount_minor": payment.amount_minor,
        "sale_receipt_id": sale.receipt_id, "items": request.basket, "profile_hash": binding.profile_hash,
    }


REFUNDABLE_STATUSES = {OrderStatus.pending, OrderStatus.confirmed, OrderStatus.paid, OrderStatus.completed}


def reserve_pending(order, row):
    return bool(row and row.bank_proof and row.receipt_id and not order.handovers
                and order.status in REFUNDABLE_STATUSES - {OrderStatus.completed})


def state(order, row, actor):
    held = reserve_pending(order, row)
    if order.handovers:
        stock_status = "received" if row and row.received_at else "awaiting_goods"
    else:
        stock_status = "reserve_pending" if held else "not_required"
    return MasterMonoRefundState(
        revision=order.revision, amount_minor=minor(order.total_amount),
        items=[{"name": item.product_name, "quantity": item.quantity} for item in order.items],
        money_status=("refunded" if row.bank_proof else "requires_review") if row else "not_started",
        receipt_status=("issued" if row.receipt_id else "pending") if row else "not_started",
        stock_status=stock_status,
        receipt_url=row.receipt_url if row else None,
        can_start=bool(settings.master_mono_refunds_enabled and not row and original(order)
                       and order.status in REFUNDABLE_STATUSES),
        can_reconcile=bool(row and (not row.bank_proof or not row.receipt_id or held)),
        can_receive=bool(row and row.bank_proof and not row.received_at and order.handovers
                         and (actor.is_superuser or actor.can_post_inventory)),
        reason=row.reason if row else None,
    )


async def read(session, actor, order_id):
    current, order, row = await context(session, actor, order_id)
    return state(order, row, current)


def observed_refund(invoice, snapshot, ext_ref, earliest):
    cancellations = invoice.get("cancelList")
    if ext_ref is None:
        # External refunds may use an unrelated reference or omit it.
        if not isinstance(cancellations, list) or len(cancellations) != 1 or not isinstance(cancellations[0], dict):
            return None
        ext_ref = cancellations[0].get("extRef")
        if ext_ref is not None and not isinstance(ext_ref, str):
            return None
    return bank_evidence(invoice, snapshot, ext_ref, earliest, datetime.now(UTC))


async def start(session, actor, order_id, payload, raw_key):
    actor, order, row = await context(session, actor, order_id)
    actor_id = actor.id
    key = operation_key(raw_key, f"pos-refund:{order_id}:{actor_id}")
    if row:
        if row.request_key == key and row.reason != payload.reason:
            fail("idempotency_conflict")
        return state(order, row, actor)
    snapshot = original(order)
    if not state(order, None, actor).can_start or not snapshot:
        fail("pos_refund_unavailable")
    if order.revision != payload.expected_revision:
        fail("revision_conflict")
    provider = master_mono.guard(session, order.master_mono_binding)
    paid_at = order.master_mono_payment.created_at
    await session.rollback()

    try:
        invoice = await provider.refund_status(snapshot["invoice_id"])
        fiscal = await provider.get_fiscal_checks(snapshot["invoice_id"])
    except MonobankError:
        fail("pos_refund_preflight_unavailable", 503)
    external = observed_refund(invoice, snapshot, None, paid_at)
    checks = fiscal["checks"]
    clean = (clean_paid_invoice(invoice, snapshot) and len(checks) == 1
             and checks[0]["type"] == "sale" and checks[0]["status"] == "done"
             and checks[0]["fiscalizationSource"] == "checkbox"
             and checks[0]["id"] == snapshot["sale_receipt_id"])

    if not clean and not (
        invoice_matches(invoice, snapshot) and (invoice.get("cancelList") or invoice.get("status") == "reversed")
    ):
        fail("pos_refund_preflight_conflict")

    actor_ref = SimpleNamespace(id=actor_id)
    actor, order, row = await context(session, actor_ref, order_id, lock=True)
    if row:
        if row.request_key == key and row.reason != payload.reason:
            fail("idempotency_conflict")
        return state(order, row, actor)
    if not state(order, None, actor).can_start or order.revision != payload.expected_revision or original(order) != snapshot:
        fail("revision_conflict")
    provider = master_mono.guard(session, order.master_mono_binding)
    ext_ref = uuid4().hex if clean else None
    session.add(MasterMonoRefund(
        order_id=order_id, operator_id=actor_id, request_key=key, ext_ref=ext_ref, reason=payload.reason,
        snapshot=snapshot, bank_proof=external, created_at=datetime.now(UTC),
    ))
    order.revision += 1
    await session.commit()
    if ext_ref:
        try:
            await provider.refund_master_invoice(
                snapshot["invoice_id"], ext_ref, snapshot["reference"], snapshot["amount_minor"], snapshot["items"],
            )
        except MonobankError:
            pass  # A durable claim owns the only POST. Reconciliation only reads provider status.
    return await reconcile(session, actor_ref, order_id)


async def reconcile(session, actor, order_id):
    current, order, row = await context(session, actor, order_id)
    if not row:
        fail("pos_refund_not_found", 404)
    snapshot = row.snapshot
    if snapshot != original(order):
        fail("pos_refund_original_changed")
    actor_id = current.id
    ext_ref = row.ext_ref
    paid_at = order.master_mono_payment.created_at
    earliest = row.created_at if ext_ref else paid_at
    provider = master_mono.guard(session, order.master_mono_binding)
    await session.rollback()
    bank = fiscal = None
    try:
        invoice = await provider.refund_status(snapshot["invoice_id"])
        bank = observed_refund(invoice, snapshot, ext_ref, earliest)
        if not bank and ext_ref:
            bank = observed_refund(invoice, snapshot, None, paid_at)
            if bank and bank["ext_ref"] == ext_ref:
                bank = None
    except MonobankError:
        pass
    try:
        fiscal = await provider.get_fiscal_checks(snapshot["invoice_id"])
    except MonobankError:
        pass
    current, order, row = await context(session, SimpleNamespace(id=actor_id), order_id, lock=True)
    if not row or row.snapshot != snapshot or row.ext_ref != ext_ref or original(order) != snapshot:
        fail("pos_refund_original_changed")
    if bank and not row.bank_proof:
        row.bank_proof = bank
    if row.bank_proof and fiscal and not row.receipt_id:
        # Mono associates checks with the exact invoice. Accept one successful return
        # only alongside the pinned sale and exact evidence of one full reversal.
        checks = fiscal["checks"]
        sales = [check for check in checks if check["type"] == "sale"]
        returns = [check for check in checks if check["type"] == "return"]
        if (len(sales) == len(returns) == 1 and sales[0]["id"] == snapshot["sale_receipt_id"]
                and all(check["status"] == "done" and check["fiscalizationSource"] == "checkbox" for check in checks)):
            row.receipt_id = returns[0]["id"]
            row.receipt_url = returns[0].get("taxUrl")
    result = state(order, row, current)
    await session.commit()
    if result.stock_status != "reserve_pending":
        return result

    # Preserve provider evidence even if the stock ledger needs reconciliation.
    from app.services.order import OrderService

    current, order, row = await context(session, SimpleNamespace(id=actor_id), order_id, lock=True)
    if reserve_pending(order, row):
        try:
            await OrderService().update_status(
                session, order_id, OrderStatus.cancelled, admin_user_id=row.operator_id, commit=False,
            )
        except HTTPException:
            await session.rollback()
            return result
    updated = state(order, row, current)
    await session.commit()
    return updated


async def receive(session, actor, order_id, payload):
    current, order, row = await context(session, actor, order_id, lock=True)
    if row and row.received_at:
        return state(order, row, current)
    if not row or not state(order, row, current).can_receive:
        fail("pos_refund_receive_unavailable")
    if order.revision != payload.expected_revision:
        fail("revision_conflict")
    movements = (await session.scalars(select(InventoryMovement).where(InventoryMovement.order_id == order_id))).all()
    shipments = {m.reference_key: m for m in movements if m.movement_type == MovementType.shipment}
    products = {p.id: p for p in (await session.scalars(select(Product).where(
        Product.id.in_({item.product_id for item in order.items})).order_by(Product.id).with_for_update()
        .execution_options(populate_existing=True))).all()}
    row.received_at, row.received_by = datetime.now(UTC), current.id
    await session.flush()
    for item in order.items:
        shipment = shipments.get(f"order:{order_id}:item:{item.id}:shipment")
        if (not shipment or shipment.order_item_id != item.id or shipment.product_id != item.product_id
                or shipment.on_hand_delta != -item.quantity or any(
                    movement.order_item_id == item.id and movement.movement_type == MovementType.customer_return
                    for movement in movements)):
            fail("pos_refund_stock_requires_review")
        products[item.product_id].stock_quantity += item.quantity
        session.add(InventoryMovement(
            order_id=order_id, order_item_id=item.id, product_id=item.product_id,
            movement_type=MovementType.customer_return, quantity=item.quantity,
            on_hand_delta=item.quantity, reserved_delta=0, performed_by_user_id=current.id,
            reference_key=f"master-mono-refund:{order_id}:item:{item.id}", reason=row.reason,
        ))
    order.revision += 1
    result = state(order, row, current)
    await session.commit()
    return result
