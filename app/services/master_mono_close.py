"""Audited close of one unpaid POS reservation after GET-only Mono reconciliation."""
import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select

from app.models.fiscal import FiscalDocument
from app.models.master_mono import MasterMonoClosedConflict, MasterMonoExpiredClosure
from app.models.order import OrderIntent, OrderItem, OrderStatus
from app.schemas.master_mono import MasterMonoClosedRecheckResult, MasterMonoCloseResult
from app.services import fiscal_automation, local_sales, master_mono
from app.services.monobank_provider import MonobankError
from app.services.order_documents import digest, operation_key


async def _admin_order(session, actor, order_id):
    actor, order = await master_mono.actor_order(session, actor, order_id, lock=True, manager=True)
    if not actor.is_superuser:
        master_mono.fail("master_mono_close_forbidden", "An administrator must close the expired sale", status=403)
    return order


async def _candidate(session, order, expected_revision):
    request = order.__dict__.get("master_mono_create_request")
    if order.revision != expected_revision:
        master_mono.fail("revision_conflict", "Reload the sale")
    if (order.simulation or not request or request.operator_id != order.created_by_user_id
            or order.status not in {OrderStatus.pending, OrderStatus.confirmed}
            or order.__dict__.get("master_mono_binding") or order.payments or order.handovers
            or order.shipments or order.qr_payment_attempts or len(order.items) != 1):
        master_mono.fail("master_mono_close_ineligible", "Sale is not an unbound single-item dispatch")
    item = order.items[0]
    if item.quantity != 1 or item.quantity_from_stock != 1 or item.quantity_received_for_order or item.quantity_to_order:
        master_mono.fail("master_mono_close_ineligible", "Sale must own one reserved stock unit")

    diagnostic = OrderIntent.response_json["dispatch_failure"]
    candidates = (await session.scalars(select(diagnostic["invoice_candidate"].as_string()).where(
        OrderIntent.order_id == order.id,
        diagnostic["stage"].as_string() == "create_response_url",
        diagnostic["http_status"].as_integer() == 200,
        diagnostic["candidate_identity_verified"].as_boolean().is_(True),
    ).distinct())).all()
    if len(candidates) != 1 or not isinstance(candidates[0], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", candidates[0]):
        master_mono.fail("master_mono_close_candidate_required", "Find and reconcile the original invoice first")
    if (await fiscal_automation.owner(session, order.id)
            or await session.scalar(select(FiscalDocument.id).where(FiscalDocument.order_id == order.id).limit(1))):
        master_mono.fail("master_mono_close_ineligible", "Another fiscal strategy owns this sale")
    await local_sales.OrderService().validate_owned_reservations(session, order, order.items)
    return request, candidates[0]


async def can_check_expired_close(session, actor, order):
    """Local eligibility only. The close command independently verifies Mono and Checkbox."""
    if not actor.is_superuser:
        return False
    try:
        request, _ = await _candidate(session, order, order.revision)
        master_mono.guard(session, request)
    except HTTPException as error:
        if error.status_code != 409:
            raise
        return False
    return True


def _fresh(when):
    return when >= datetime.now(UTC) - timedelta(seconds=30)


def _bank_matches(request, candidate, proof):
    return (master_mono.matching_identity(proof, request) and proof.get("invoice_id") == candidate
        and master_mono.dated_recovery(proof, request) and proof.get("currency_code") == 980
        and _fresh(datetime.fromisoformat(proof["observed_at"])))


def _fiscal_matches(identity, fiscal):
    return (isinstance(fiscal, dict) and fiscal.get("verification_scope") == "provider_invoice_association"
        and isinstance(fiscal.get("checks"), list)
        and fiscal.get("proof_hash") == digest({k: v for k, v in fiscal.items() if k != "proof_hash"})
        and all(fiscal.get(k) == identity[k] for k in (
            "verified", "invoice_id", "merchant_id", "profile_id", "authority", "transport",
        )))


def _bank_contradicts_expiry(proof):
    return (proof.get("status") != "expired" or proof.get("has_cancellations") is not False
        or proof.get("final_amount_minor") not in (None, 0))


def _check_evidence(request, candidate, proof, fiscal):
    if not _bank_matches(request, candidate, proof) or _bank_contradicts_expiry(proof):
        master_mono.fail("master_mono_close_unpaid_unproven", "Mono did not confirm this exact expired unpaid invoice")
    if not _fiscal_matches(proof, fiscal) or fiscal["checks"]:
        master_mono.fail("master_mono_close_fiscal_unproven", "Mono did not confirm the absence of associated receipts")


def _replay(closure, key, payload):
    if closure.operation_key != key or closure.expected_revision != payload.expected_revision:
        master_mono.fail("master_mono_sale_closed", "This sale already has an audited close")
    return MasterMonoCloseResult(order_id=closure.order_id, revision=closure.expected_revision + 1)


async def close(session, actor, order_id, payload, raw_key):
    actor_id = actor.id
    expected_revision = payload.expected_revision
    try:
        order = await _admin_order(session, actor, order_id)
        key = operation_key(raw_key, f"master-mono-close-expired:{actor_id}:{order_id}")
        existing = await session.get(MasterMonoExpiredClosure, order_id)
        if existing:
            result = _replay(existing, key, payload)
            await session.rollback()
            return result
        request, candidate = await _candidate(session, order, expected_revision)
        provider = master_mono.guard(session, request)
        request_id = request.id
        await session.rollback()
        await master_mono.lock_invoice_reconciliation(session, order_id)
        existing = await session.get(MasterMonoExpiredClosure, order_id)
        if existing:
            await _admin_order(session, SimpleNamespace(id=actor_id), order_id)
            result = _replay(existing, key, payload)
            await session.rollback()
            return result

        try:
            # Receipt association is checked first, then the exact invoice status is read just before closing.
            fiscal_requested_at = datetime.now(UTC)
            fiscal = await provider.get_fiscal_checks(candidate)
            proof = await provider.inspect_invoice(candidate)
        except MonobankError:
            master_mono.fail("master_mono_close_verification_unavailable", "Could not reconcile Mono and Checkbox evidence", status=503)

        # Match the stock service lock order: items, order, then products.
        await session.execute(select(OrderItem.id).where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id).with_for_update())
        order = await _admin_order(session, SimpleNamespace(id=actor_id), order_id)
        existing = await session.get(MasterMonoExpiredClosure, order_id)
        if existing:
            result = _replay(existing, key, payload)
            await session.rollback()
            return result
        request, current_candidate = await _candidate(session, order, expected_revision)
        master_mono.guard(session, request)
        if request.id != request_id or current_candidate != candidate:
            master_mono.fail("master_mono_close_candidate_conflict", "The original dispatch changed")
        _check_evidence(request, candidate, proof, fiscal)
        if fiscal_requested_at < datetime.now(UTC) - timedelta(seconds=30):
            master_mono.fail("master_mono_close_fiscal_unproven", "Read fresh Mono receipt evidence")
        session.add(MasterMonoExpiredClosure(order_id=order_id, request_id=request_id, actor_id=actor_id,
            operation_key=key, bank_evidence_id=proof["proof_hash"],
            expected_revision=expected_revision, proof=proof, fiscal_proof=fiscal,
            fiscal_requested_at=fiscal_requested_at, created_at=datetime.now(UTC)))
        await session.flush()
        closed = await local_sales.OrderService().update_status(session, order_id, OrderStatus.cancelled,
            admin_user_id=actor_id, commit=False)
        if fiscal_requested_at < datetime.now(UTC) - timedelta(seconds=30):
            master_mono.fail("master_mono_close_fiscal_unproven", "Read fresh Mono receipt evidence")
        result = MasterMonoCloseResult(order_id=order_id, revision=closed.revision)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def _read_late_evidence(read, invoice):
    try:
        async with asyncio.timeout(24):
            return await read(invoice)
    except (MonobankError, TimeoutError):
        return None


async def recheck(session, actor, order_id, payload, raw_key):
    """Quarantine observed contradictions without reopening or settling the sale."""
    actor_id = actor.id
    try:
        await master_mono.lock_invoice_reconciliation(session, order_id)
        order = await _admin_order(session, actor, order_id)
        closure = await session.get(MasterMonoExpiredClosure, order_id)
        if not closure or order.status != OrderStatus.cancelled:
            master_mono.fail("master_mono_closed_recheck_required", "This sale has no audited expiry closure")
        if order.revision != payload.expected_revision:
            master_mono.fail("revision_conflict", "Reload the sale")
        key = operation_key(raw_key, f"master-mono-closed-recheck:{actor_id}:{order_id}")
        quarantined = await master_mono.has_closed_conflict(session, order_id)
        if await session.get(MasterMonoClosedConflict, key):
            await session.rollback()
            return MasterMonoClosedRecheckResult(order_id=order_id, revision=payload.expected_revision,
                status="reconciliation_required")

        request = await session.get(master_mono.CreateRequest, closure.request_id)
        provider = master_mono.guard(session, request)
        identity = closure.proof
        invoice = identity["invoice_id"]
        requested_at = datetime.now(UTC)
        proof, fiscal = await asyncio.gather(
            _read_late_evidence(provider.inspect_invoice, invoice),
            _read_late_evidence(provider.get_fiscal_checks, invoice),
        )
        if not _bank_matches(request, invoice, proof) or proof["created_at"] != identity["created_at"]:
            proof = None
        if not _fiscal_matches(identity, fiscal):
            fiscal = None

        bank_conflict = proof is not None and _bank_contradicts_expiry(proof)
        fiscal_conflict = fiscal is not None and bool(fiscal["checks"])
        if not _fresh(requested_at) or (not (bank_conflict or fiscal_conflict) and (proof is None or fiscal is None)):
            master_mono.fail("master_mono_closed_recheck_unavailable", "Could not verify the exact closed invoice", status=503)
        if bank_conflict or fiscal_conflict:
            if fiscal is not None:
                fiscal = {**fiscal, "checks": [
                    {k: check[k] for k in ("id", "status", "type", "fiscalizationSource")} for check in fiscal["checks"]
                ]}
                fiscal["proof_hash"] = digest({k: v for k, v in fiscal.items() if k != "proof_hash"})
            session.add(MasterMonoClosedConflict(operation_key=key, order_id=order_id, actor_id=actor_id,
                proof=proof, fiscal_proof=fiscal, requested_at=requested_at, created_at=datetime.now(UTC)))
            await session.commit()
            quarantined = True
        else:
            await session.rollback()
        return MasterMonoClosedRecheckResult(order_id=order_id, revision=payload.expected_revision,
            status="reconciliation_required" if quarantined else "cancelled")
    except Exception:
        await session.rollback()
        raise
