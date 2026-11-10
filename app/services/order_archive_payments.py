"""Reconcile verified Mono money against an archived order without reopening it."""

from datetime import UTC, datetime
import logging
from types import SimpleNamespace

from sqlalchemy import exists, select

from app.core import error_reporting
from app.models.order import OrderStatus
from app.models.shop_sales import ShopOperationAudit
from app.services import shop_mono_checkbox as direct, shop_payments as payments, shop_sales as sales
from app.services.order_documents import digest


logger = logging.getLogger(__name__)


async def is_owner_archived(session, order):
    return order.status == OrderStatus.cancelled and bool(await session.scalar(select(exists().where(
        ShopOperationAudit.order_id == order.id, ShopOperationAudit.operation == "owner_cancel_order",
    ))))


async def dispatch_started(session, request):
    audit = await session.scalar(select(ShopOperationAudit).where(
        ShopOperationAudit.order_id == request.order_id,
        ShopOperationAudit.operation == "online_dispatch_started",
        ShopOperationAudit.request_key == digest(["shop-online-dispatch", request.id]),
        ShopOperationAudit.operator_id == request.operator_id,
    ))
    return (audit is not None and isinstance(audit.facts, dict)
        and type(audit.facts.get("request_id")) is int
        and audit.facts == {"request_id": request.id, "intent_id": request.intent_id})


async def refresh(session, actor, order):
    """Poll a pinned invoice using GET only. Callers own retries and failure reporting."""
    if not await is_owner_archived(session, order) or order.payments:
        return
    order_id, actor_id = order.id, actor.id
    actor = await sales.current_actor(session, actor, "read", order)
    binding = order.shop_payment_intent
    if not binding or not binding.invoice_id:
        return
    provider = await direct.guard(session, order)
    from app.services.shop_payment_resolutions import effective_invoice
    invoice, _ = await effective_invoice(session, binding)
    binding_id = binding.id
    reference, amount = binding.reference, binding.amount_minor
    await session.rollback()

    proof = await provider.get_invoice(invoice, reference=reference, amount_minor=amount)
    if proof["status"] != "success":
        return

    order = await sales.load_order(session, order_id, lock=True)
    actor = await sales.current_actor(session, SimpleNamespace(id=actor_id), "read", order)
    binding = order.shop_payment_intent
    if not binding or binding.id != binding_id:
        sales.fail("invoice_binding_conflict", "Reload the original archived invoice")
    await direct.guard(session, order)
    try:
        if not await record_verified(session, actor, order, proof):
            return
        await session.commit()
    except Exception as error:
        await session.rollback()
        error_reporting.report_exception("paid_save", error, order_id=order_id)
        raise
    logger.info("owner_cancel_late_payment_verified order_id=%d", order_id)


async def record_verified(session, actor, order, proof):
    """Stage exact paid evidence under the caller's order lock."""
    if not await is_owner_archived(session, order) or order.payments:
        return False
    binding = order.shop_payment_intent
    from app.services.shop_payment_resolutions import effective_invoice
    invoice, correction = await effective_invoice(session, binding)
    if not direct.invoice_valid(proof, binding, invoice) or not payments.invoice_proof_dates_valid(proof, binding):
        sales.fail("monobank_proof_invalid", "Late payment proof does not match the original invoice")

    key = digest(["owner-cancel-late-payment", binding.id, invoice])
    existing = await session.scalar(select(ShopOperationAudit.id).where(ShopOperationAudit.request_key == key))
    if existing is not None:
        return False
    await payments.record(session, order, actor, binding, proof, authority=proof["authority"], source="provider",
        reference=invoice, identity=digest([binding.profile_id, invoice]), method="card",
        correction_id=correction.id if correction else None, preserve_cancelled=True)
    request = order.shop_online_payment_request
    session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id, operation="owner_cancel_late_payment",
        request_key=key, facts={"request_id": request.id if request else None, "intent_id": binding.id,
            "invoice_id": invoice, "proof": proof}, created_at=datetime.now(UTC)))
    return True
