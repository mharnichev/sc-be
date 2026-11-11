"""Direct Mono→Checkbox refund workflow behind an isolated test-only transport.

Mono fiscal-checks associates receipts with the invoice. It supplies no refund
amount, extRef or original receipt identity. A separate exact receipt verifier
is mandatory for finalization. The default gateway is unavailable.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.order import OrderStatus
from app.models.shop_mono_checkbox_refunds import ShopMonoCheckboxRefund
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_mono_checkbox_refunds import DirectFiscalEntry, DirectFiscalReport, DirectRefundState, RefundableItem
from app.services import shop_mono_checkbox as direct, shop_payment_resolutions as resolutions, shop_sales as sales
from app.services.monobank_provider import MonobankError, invoice_payload
from app.services.order import OrderService
from app.services.order_documents import digest, minor, operation_key


class MockRefundGateway:
    def __init__(self, transport, linked_receipt):
        if type(transport) is not httpx.MockTransport:
            raise TypeError("Direct refund gateway requires an HTTP mock")
        self.transport = transport
        self.linked_receipt = linked_receipt

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(transport=self.transport, base_url="https://monobank.invalid",
                                    follow_redirects=False, trust_env=False, timeout=5) as client:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

    async def cancel(self, snapshot, ext_ref):
        payload = {"invoiceId": snapshot["invoice_id"], "extRef": ext_ref, "items": snapshot["items"]}
        if snapshot["kind"] == "partial":
            payload["amount"] = snapshot["amount_minor"]
        await self.request("POST", "/api/merchant/invoice/cancel", json=payload)

    async def status(self, invoice_id):
        return await self.request("GET", "/api/merchant/invoice/status", params={"invoiceId": invoice_id})


def gateway():
    return None


async def enabled_gateway(session):
    provider = gateway()
    if (not settings.shop_order_operations_enabled or not settings.shop_mono_checkbox_enabled
            or not settings.shop_mono_checkbox_refunds_enabled or type(provider) is not MockRefundGateway
            or not callable(provider.linked_receipt)):
        sales.fail("direct_refunds_unavailable", "Direct refund verification is unavailable", 503)
    await sales.require_test_database(session, "direct_refund_test_unsafe")
    return provider


async def record(session, order_id):
    return await session.scalar(select(ShopMonoCheckboxRefund).where(ShopMonoCheckboxRefund.order_id == order_id)
                                .execution_options(populate_existing=True))


async def active(session, order_id):
    return await record(session, order_id) is not None


def eligible(order):
    resolutions.eligible(order)
    confirmation = sales.canonical_payment(order)
    if (order.status != OrderStatus.paid or not confirmation or resolutions.cases(order)
            or confirmation.snapshot.get("original_payment_method") != "onlineCard"):
        sales.fail("direct_refund_ineligible", "An exclusive paid direct Shop order is required")
    if order.tracking_number or any(shipment.active for shipment in order.shipments):
        sales.fail("direct_refund_active_shipment", "Reconcile active shipment before refunding")


def original(order):
    reasons = direct.configuration_reasons(order)
    if reasons:
        sales.fail(reasons[0], "Restore the pinned direct Mono→Checkbox profile")
    sale = direct.accepted_receipt(order)
    attempt = order.shop_online_payment_request
    binding = order.shop_payment_intent
    if (not sale or not attempt or not binding or binding.authority != "test" or binding.transport != "mock_http"
            or attempt.intent_id != binding.id or attempt.reference != binding.reference
            or attempt.amount_minor != binding.amount_minor or attempt.profile_hash != binding.profile_hash
            or attempt.basket_hash != digest(attempt.basket)):
        sales.fail("direct_refund_original_required", "The exact original sale and server basket are required")
    return sale, attempt


def verified_basket(attempt):
    try:
        return invoice_payload(attempt.reference, attempt.amount_minor, attempt.basket)["merchantPaymInfo"]["basketOrder"]
    except MonobankError:
        sales.fail("direct_refund_basket_invalid", "The original refund basket is invalid")


async def carrier_basis(session, order, receipt_id):
    if receipt_id is None:
        eligible(order)
        return None
    from app.services.shop_np_direct_returns import financial_basis
    return await financial_basis(session, order, receipt_id)


def carrier_items(order, basket, basis):
    """Allocate only received units against the immutable bank basket."""
    products = {str(item.product_id): item for item in order.items}
    if len(products) != len(order.items):
        sales.fail("direct_refund_basket_invalid", "Ambiguous original basket product identity")
    counts = {str(item.id): 0 for item in order.items}
    remaining = dict(basis["quantities"])
    items = []
    for item in basket:
        original = products.get(item["code"])
        if original is None:
            sales.fail("direct_refund_basket_invalid", "Unknown product in original bank basket")
        key = str(original.id)
        counts[key] += item["qty"]
        quantity = min(item["qty"], remaining.get(key, 0))
        if quantity:
            items.append({**item, "qty": quantity, "total": quantity * item["sum"]})
            remaining[key] -= quantity
    if any(remaining.values()) or counts != {str(item.id): item.quantity for item in order.items}:
        sales.fail("direct_refund_basket_invalid", "Received quantities must match original bank basket lines")
    return items


def snapshot_for(order, payload, carrier=None):
    sale, attempt = original(order)
    basket = verified_basket(attempt)
    items = basket
    if carrier:
        if payload.quantities:
            sales.fail("direct_refund_carrier_quantities", "Use the exact physically received quantities")
        items = carrier_items(order, basket, carrier)
    elif payload.kind == "partial":
        items = []
        for index, quantity in sorted(payload.quantities.items()):
            if not 0 <= index < len(basket) or not 1 <= quantity <= basket[index]["qty"]:
                sales.fail("direct_refund_basket_invalid", "Select valid quantities from the original basket")
            item = basket[index]
            items.append({**item, "qty": quantity, "total": quantity * item["sum"]})
    amount = sum(item["total"] for item in items)
    if (payload.kind == "full" and (payload.quantities or amount != attempt.amount_minor)
            or payload.kind == "partial" and not 0 < amount < attempt.amount_minor):
        sales.fail("direct_refund_basket_invalid", "Partial refund must be a proper subset of the original basket")
    return {
        "kind": payload.kind, "invoice_id": sale.invoice_id, "reference": attempt.reference,
        "sale_id": sale.id, "sale_receipt_id": sale.receipt_id, "sale_snapshot_hash": sale.snapshot_hash,
        "profile_hash": attempt.profile_hash, "merchant_id": sale.snapshot["merchant_id"],
        "invoice_amount_minor": attempt.amount_minor, "amount_minor": amount, "items": items,
        "basket_hash": attempt.basket_hash, "items_hash": digest(items),
        "request_hash": digest(payload.model_dump(mode="json", exclude_none=True)), "authority": "test",
        **({"carrier_receipt": carrier} if carrier else {}),
        "reserve_plan": [
            {"order_item_id": item.id, "product_id": item.product_id,
             "quantity": item.quantity_from_stock + item.quantity_received_for_order}
            for item in sorted(order.items, key=lambda item: item.id)
        ],
    }


def invoice_matches(status, snapshot):
    return (isinstance(status, dict) and status.get("invoiceId") == snapshot["invoice_id"]
            and status.get("reference") == snapshot["reference"] and type(status.get("ccy")) is int
            and status["ccy"] == 980 and type(status.get("amount")) is int
            and status["amount"] == snapshot["invoice_amount_minor"] and type(status.get("finalAmount")) is int)


def clean_paid_invoice(status, snapshot):
    return (invoice_matches(status, snapshot) and status.get("status") == "success"
            and status["finalAmount"] == snapshot["invoice_amount_minor"] and status.get("cancelList", []) == [])


def bank_evidence(status, snapshot, ext_ref, created_at, observed_at):
    if not invoice_matches(status, snapshot):
        return None
    cancellations = status.get("cancelList")
    if not isinstance(cancellations, list) or len(cancellations) != 1 or not isinstance(cancellations[0], dict):
        return None
    row = cancellations[0]
    remainder = snapshot["invoice_amount_minor"] - snapshot["amount_minor"]
    expected_status = "reversed" if remainder == 0 else "success"
    if (row.get("extRef") != ext_ref or row.get("status") != "success"
            or type(row.get("amount")) is not int or row["amount"] != snapshot["amount_minor"]
            or type(row.get("ccy")) is not int or row["ccy"] != 980
            or status["finalAmount"] != remainder or status.get("status") != expected_status):
        return None
    try:
        created, modified, invoice_modified = (
            datetime.fromisoformat(value) for value in (row["createdDate"], row["modifiedDate"], status["modifiedDate"])
        )
        if not created_at - timedelta(seconds=1) <= created <= modified <= invoice_modified <= observed_at + timedelta(seconds=30):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "invoice_id": snapshot["invoice_id"], "reference": snapshot["reference"], "ext_ref": ext_ref,
        "amount_minor": row["amount"], "remaining_minor": remainder, "currency": "UAH",
        "status": expected_status, "modified_at": modified.isoformat(), "observed_at": observed_at.isoformat(),
    }


def receipt_evidence(proof, snapshot, ext_ref):
    expected = {
        "invoice_id": snapshot["invoice_id"], "original_receipt_id": snapshot["sale_receipt_id"],
        "ext_ref": ext_ref, "amount_minor": snapshot["amount_minor"], "currency": "UAH",
        "profile_hash": snapshot["profile_hash"], "merchant_id": snapshot["merchant_id"],
        "items_hash": snapshot["items_hash"], "issuer": "checkbox", "type": "return", "status": "done",
    }
    if (not isinstance(proof, dict) or type(proof.get("amount_minor")) is not int
            or any(proof.get(key) != value for key, value in expected.items())):
        return None
    try:
        identifier = str(UUID(proof["receipt_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if identifier != proof["receipt_id"] or identifier == snapshot["sale_receipt_id"]:
        return None
    return {**expected, "receipt_id": identifier}


def report(order, refund):
    sale = direct.accepted_receipt(order)
    if not sale:
        return None
    amount = sale.snapshot["amount_minor"]
    entries = [DirectFiscalEntry(kind="sale", receipt_id=sale.receipt_id, amount_minor=amount,
                                 structured_totals_verified=False)]
    if refund and refund.finalized_at and refund.sale_id == sale.id:
        returned = refund.snapshot["amount_minor"]
        entries.append(DirectFiscalEntry(kind="return", receipt_id=refund.return_receipt_id,
                                        original_receipt_id=sale.receipt_id, amount_minor=returned,
                                        structured_totals_verified=True))
        amount -= returned
    return DirectFiscalReport(authority=sale.authority, entries=entries, net_minor=amount)


def state(order, refund, *, available=False, reason="direct_refunds_unavailable", basket=(), carrier_id=None, carrier_amount=None):
    if refund is None:
        return DirectRefundState(
            order_id=order.id, revision=order.revision, available=available, can_start=available,
            reason=reason, report=report(order, refund), carrier_receipt_id=carrier_id,
            amount_minor=carrier_amount,
            kind=("full" if carrier_amount == minor(order.total_amount) else "partial") if carrier_amount else None,
            refundable_items=[
                RefundableItem(basket_index=index, name=item["name"], quantity=item["qty"], unit_amount_minor=item["sum"])
                for index, item in enumerate(basket)
            ],
        )
    return DirectRefundState(
        order_id=order.id, revision=order.revision, available=available, can_reconcile=available,
        carrier_receipt_id=refund.snapshot.get("carrier_receipt", {}).get("receipt_id"),
        kind=refund.snapshot["kind"], amount_minor=refund.snapshot["amount_minor"], ext_ref=refund.ext_ref,
        bank_confirmed=refund.bank_proof is not None, return_receipt_id=refund.return_receipt_id,
        finalized_at=refund.finalized_at, last_checked_at=refund.last_checked_at, reason=refund.reason,
        report=report(order, refund),
    )


async def read(session, actor, order_id):
    order = await sales.load_order(session, order_id)
    carrier_dispatch = order.fulfillment_method == "nova_poshta" and bool(sales.canonical_fulfillment(order))
    await sales.current_actor(session, actor, "refund" if carrier_dispatch else "handover", order)
    refund = await record(session, order_id)
    basket, carrier_id, carrier_amount = [], None, None
    try:
        await enabled_gateway(session)
        if not refund:
            if carrier_dispatch:
                from app.services.shop_np_direct_returns import latest_receipt_id
                carrier_id = await latest_receipt_id(session, order.id)
            carrier = await carrier_basis(session, order, carrier_id)
            _, attempt = original(order)
            if carrier:
                carrier_amount = sum(i["total"] for i in carrier_items(order, verified_basket(attempt), carrier))
            else:
                basket = verified_basket(attempt)
    except HTTPException as error:
        return state(order, refund, reason=error.detail["code"])
    return state(order, refund, available=not refund or not refund.finalized_at, reason="ready", basket=basket, carrier_id=carrier_id, carrier_amount=carrier_amount)


def audit(session, refund, actor_id, operation, key, facts):
    session.add(ShopOperationAudit(order_id=refund.order_id, operator_id=actor_id, operation=operation,
                                   request_key=key, facts={"refund_id": refund.id, **facts},
                                   created_at=datetime.now(UTC)))


def existing_result(order, refund, key, payload):
    if refund.request_key != key or refund.snapshot["request_hash"] != digest(payload.model_dump(mode="json", exclude_none=True)):
        sales.fail("direct_refund_already_claimed", "Reconcile the existing refund reference")
    return state(order, refund, available=not refund.finalized_at)


async def start(session, actor, order_id, payload, raw_key):
    provider = await enabled_gateway(session)
    actor_id = actor.id
    key = operation_key(raw_key, f"direct-refund:{order_id}:{actor_id}")
    order = await sales.load_order(session, order_id)
    operation = "refund" if payload.carrier_receipt_id else "handover"
    await sales.current_actor(session, actor, operation, order)
    refund = await record(session, order_id)
    if refund:
        return existing_result(order, refund, key, payload)
    carrier = await carrier_basis(session, order, payload.carrier_receipt_id)
    snapshot = snapshot_for(order, payload, carrier)
    if order.revision != payload.expected_revision:
        sales.fail("revision_conflict", "Reload order")
    await session.rollback()
    try:
        invoice = await provider.status(snapshot["invoice_id"])
    except (httpx.HTTPError, ValueError):
        sales.fail("direct_refund_bank_unavailable", "Bank preflight failed", 503)
    try:
        order = await sales.load_order(session, order_id, lock=True)
        await sales.current_actor(session, SimpleNamespace(id=actor_id), operation, order)
        refund = await record(session, order_id)
        if refund:
            result = existing_result(order, refund, key, payload)
            await session.rollback()
            return result
        carrier = await carrier_basis(session, order, payload.carrier_receipt_id)
        if not clean_paid_invoice(invoice, snapshot):
            sales.fail("direct_refund_bank_conflict", "Bank preflight requires one exact paid invoice without prior refunds")
        if order.revision != payload.expected_revision or snapshot != snapshot_for(order, payload, carrier):
            sales.fail("revision_conflict", "Reload order")
        if not carrier:
            await OrderService.validate_owned_reservations(session, order, order.items)
        await enabled_gateway(session)
        ext_ref = uuid4().hex
        refund = ShopMonoCheckboxRefund(order_id=order_id, sale_id=snapshot["sale_id"], operator_id=actor_id,
                                        ext_ref=ext_ref, request_key=key, snapshot=snapshot, snapshot_hash=digest(snapshot),
                                        reason="dispatch_outcome_unknown", created_at=datetime.now(UTC))
        session.add(refund)
        order.revision += 1
        await session.flush()
        audit(session, refund, actor_id, "direct_refund_claim", key, {"snapshot_hash": refund.snapshot_hash, "ext_ref": ext_ref})
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    try:
        await provider.cancel(snapshot, ext_ref)
    except (httpx.HTTPError, ValueError):
        pass  # The claim already owns the only dispatch. Recovery reads status.
    return await read(session, SimpleNamespace(id=actor_id), order_id)


async def reconcile(session, actor, order_id):
    provider = await enabled_gateway(session)
    actor_id = actor.id
    order = await sales.load_order(session, order_id)
    refund = await record(session, order_id)
    operation = "refund" if refund and refund.snapshot.get("carrier_receipt") else "handover"
    await sales.current_actor(session, actor, operation, order)
    if not refund:
        sales.fail("direct_refund_missing", "Start a direct refund before reconciliation", 404)
    if refund.finalized_at:
        return state(order, refund)
    snapshot, ext_ref, created_at = refund.snapshot, refund.ext_ref, refund.created_at
    await session.rollback()

    try:
        status = await provider.status(snapshot["invoice_id"])
        bank_reason = "bank_reconciliation_required"
    except (httpx.HTTPError, ValueError):
        status, bank_reason = None, "bank_unavailable"
    observed_at = datetime.now(UTC)
    bank = bank_evidence(status, snapshot, ext_ref, created_at, observed_at)
    try:
        proof = await provider.linked_receipt(snapshot, ext_ref)
        receipt_reason = "linked_return_receipt_required"
    except (httpx.HTTPError, ValueError):
        proof, receipt_reason = None, "linked_return_receipt_unavailable"
    receipt = receipt_evidence(proof, snapshot, ext_ref)

    try:
        order = await sales.load_order(session, order_id, lock=True)
        actor = await sales.current_actor(session, SimpleNamespace(id=actor_id), operation, order)
        refund = await record(session, order_id)
        await enabled_gateway(session)
        if refund.finalized_at or refund.last_checked_at and refund.last_checked_at >= observed_at:
            result = state(order, refund, available=not refund.finalized_at)
            await session.rollback()
            return result
        expected_carrier = snapshot.get("carrier_receipt")
        carrier = await carrier_basis(session, order, expected_carrier["receipt_id"] if expected_carrier else None)
        if carrier != expected_carrier:
            sales.fail("direct_refund_proof_conflict", "The physical receipt no longer matches the refund")
        sale, attempt = original(order)
        if (refund.ext_ref != ext_ref or refund.snapshot_hash != digest(snapshot)
                or sale.id != refund.sale_id or sale.snapshot_hash != snapshot["sale_snapshot_hash"]
                or attempt.basket_hash != snapshot["basket_hash"]):
            sales.fail("direct_refund_proof_conflict", "Refund facts no longer match the original sale")
        if receipt and refund.receipt_proof and refund.receipt_proof != receipt:
            receipt, receipt_reason = None, "linked_return_receipt_conflict"
        if bank and not refund.bank_proof:
            refund.bank_proof = bank
        if receipt and not refund.receipt_proof:
            refund.receipt_proof = receipt
            refund.return_receipt_id = receipt["receipt_id"]
        refund.last_checked_at = observed_at
        refund.reason = bank_reason if not bank else receipt_reason
        facts = {"bank": bank, "receipt": receipt, "reason": refund.reason}
        if bank and receipt:
            if carrier:
                refund.reason = "carrier_full_refund_finalized" if snapshot["kind"] == "full" else "carrier_partial_refund_finalized"
            elif snapshot["kind"] == "full":
                await session.flush()  # Persist both proofs before touching inventory.
                facts["reserve_release"] = await resolutions.release(
                    session, order, refund, actor, reference_prefix="shop-direct-refund")
                await session.flush()  # Closure validates the actual ledger releases.
                refund.reason = "full_refund_finalized"
            else:
                refund.reason = "partial_refund_finalized"
            refund.finalized_at = datetime.now(UTC)
            facts["reason"] = refund.reason
            await session.flush()
            if snapshot["kind"] == "full" and not carrier:
                order.status = OrderStatus.cancelled
        order.revision += 1
        audit(session, refund, actor_id, "direct_refund_reconcile", uuid4().hex, facts)
        result = state(order, refund, available=not refund.finalized_at)
        await session.commit()
        return result
    except IntegrityError:
        await session.rollback()
        sales.fail("direct_refund_identity_conflict", "An external return receipt already belongs to another refund")
    except Exception:
        await session.rollback()
        raise
