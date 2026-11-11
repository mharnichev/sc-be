"""Read-only customer projection of immutable direct NP return facts."""
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.models.shop_np_direct_returns import ShopNPDirectReturnObservation
from app.schemas.customer_return import CustomerDirectReturn
from app.services import shop_mono_checkbox as direct
from app.services import shop_mono_checkbox_refunds as refunds
from app.services import shop_np_direct_returns as physical
from app.services import shop_sales as sales
from app.services.order_documents import digest


def recent(value, now, max_age):
    return bool(value and now - max_age
                <= sales.aware(value) <= now + timedelta(seconds=30))


def valid_bank(proof, snapshot, ext_ref):
    amount, original = snapshot["amount_minor"], snapshot["invoice_amount_minor"]
    remaining = original - amount
    return bool(isinstance(proof, dict)
        and type(proof.get("amount_minor")) is int and proof["amount_minor"] == amount
        and type(proof.get("remaining_minor")) is int and proof["remaining_minor"] == remaining
        and proof.get("invoice_id") == snapshot["invoice_id"] and proof.get("reference") == snapshot["reference"]
        and proof.get("ext_ref") == ext_ref and proof.get("currency") == "UAH"
        and proof.get("status") == ("reversed" if not remaining else "success"))


async def project(session, order):
    """Call only after owner or guest tracking authorization."""
    if getattr(order, "fulfillment_method", None) != "nova_poshta" or not order.__dict__.get("shop_mono_checkbox_receipt"):
        return None
    case = await physical.case_for(session, order.id)
    if not case or case.snapshot_hash != digest(case.snapshot) or case.snapshot.get("authority") != "test":
        return None
    if "items" not in order.__dict__:
        await session.refresh(order, ["items"])
    fulfillment = sales.canonical_fulfillment(order)
    shipment = next((row for row in order.shipments if row.id == case.original_shipment_id), None)
    sale = direct.accepted_receipt(order)
    if not fulfillment or fulfillment.kind != "nova_poshta" or not shipment or not sale or fulfillment.shipment_id != shipment.id:
        return None
    try:
        physical.verify_binding(case, physical.binding(order, shipment, fulfillment, sale))
    except HTTPException:
        return None
    now = datetime.now(UTC)
    receipt = await physical.receipt_for(session, order.id)
    stmt = select(ShopNPDirectReturnObservation).where(ShopNPDirectReturnObservation.case_id == case.id)
    if receipt:
        stmt = stmt.where(ShopNPDirectReturnObservation.id == receipt.observation_id)
    observation = await session.scalar(stmt.order_by(ShopNPDirectReturnObservation.id.desc()).limit(1))
    proof = observation.proof if observation and isinstance(observation.proof, dict) else {}
    valid_observation = bool(observation and observation.proof_hash == digest(proof)
        and proof.get("verified") is True and proof.get("authority") == "test"
        and proof.get("original_ttn") == case.original_ttn and proof.get("return_ttn") == case.return_ttn)
    reverse = proof.get("reverse") if valid_observation else None
    reverse = reverse if isinstance(reverse, dict) else {}
    delivered = reverse.get("status") == "delivered" and bool(reverse.get("event_at"))
    if receipt and valid_observation and delivered and receipt.case_id == case.id:
        recorded = receipt.snapshot
        valid_receipt = (receipt.proof_hash == digest(recorded)
            and recorded == physical.receipt_snapshot(receipt, case, observation, recorded["lines"], recorded["evidence"])
            and receipt.quantities == physical.quantities(recorded["lines"]))
        if valid_receipt:
            result = CustomerDirectReturn(inspection_status="received", received_quantity=sum(receipt.quantities.values()))
            return await project_financial(session, order, case, receipt, sale, now, result)
    try:
        checked_at = datetime.fromisoformat(proof.get("observed_at", ""))
    except (ValueError, TypeError):
        checked_at = None
    stale = not valid_observation or not recent(checked_at, now,
        timedelta(seconds=min(settings.np_tracking_stale_seconds, 7200)))
    return CustomerDirectReturn(
        inspection_status="awaiting_confirmation" if stale or receipt else "awaiting_inspection" if delivered else "awaiting_parcel",
        tracking_stale=stale,
    )


async def project_financial(session, order, case, receipt, sale, now, result):
    refund = await refunds.record(session, order.id)
    if not refund:
        return result
    snapshot = refund.snapshot
    expected = {
        "receipt_id": str(receipt.id), "quantities": receipt.quantities, "proof_hash": receipt.proof_hash,
        "original_ttn": case.original_ttn, "return_ttn": case.return_ttn,
    }
    if (refund.snapshot_hash != digest(snapshot) or snapshot.get("carrier_receipt") != expected
            or refund.sale_id != sale.id or snapshot.get("sale_snapshot_hash") != sale.snapshot_hash
            or snapshot.get("invoice_id") != sale.invoice_id or snapshot.get("sale_receipt_id") != sale.receipt_id):
        return result
    amount, original = snapshot.get("amount_minor"), snapshot.get("invoice_amount_minor")
    kind = snapshot.get("kind")
    if (type(amount) is not int or type(original) is not int or not 0 < amount <= original
            or original != sale.snapshot["amount_minor"]
            or kind != ("full" if amount == original else "partial")):
        return result
    try:
        expected_items = refunds.carrier_items(order, refunds.verified_basket(order.shop_online_payment_request), expected)
    except HTTPException:
        return result
    if snapshot.get("items") != expected_items or amount != sum(item["total"] for item in expected_items):
        return result
    bank = valid_bank(refund.bank_proof, snapshot, refund.ext_ref)
    check = refunds.receipt_evidence(refund.receipt_proof, snapshot, refund.ext_ref)
    receipt_ok = bool(check and check["receipt_id"] == refund.return_receipt_id)
    return result.model_copy(update={
        "refund_kind": kind,
        "money_status": "confirmed" if bank else "pending",
        "refunded_amount_minor": amount if bank else None,
        "currency": "UAH" if bank else None,
        "return_receipt_status": "confirmed" if receipt_ok else "pending",
        "financial_stale": not (refund.finalized_at and bank and receipt_ok) and (
            not recent(refund.last_checked_at, now, timedelta(hours=1))
            or refund.reason in {"bank_unavailable", "bank_reconciliation_required", "linked_return_receipt_unavailable", "linked_return_receipt_conflict"}
        ),
    })
