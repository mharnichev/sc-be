"""Import existing mono→Checkbox receipts; never charge or issue fiscal documents.

Fiscal-checks proves provider invoice association only. Canonical R2 proves the
paid order amount; this endpoint provides no structured receipt totals or X/Z.
"""
from app.core import error_reporting
from datetime import UTC, datetime
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.order import OrderIntent
from app.models.shop_mono_checkbox import ShopMonoCheckboxReceipt as Receipt, ShopMonoCheckboxObservation as Observation, ShopMonoCheckboxArtifact as Artifact
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_mono_checkbox import DirectReceiptState
from app.services import shop_sales as sales, shop_payments as payments
from app.services.order_documents import claim_intent, digest, operation_key


def internal_facts(order):
    return bool(order and any(order.__dict__.get(name) for name in (
        "shop_checkbox_binding", "shop_checkbox_documents", "shop_fiscal_documents",
        "shop_financial_returns", "shop_carrier_returns", "shop_checkbox_returns")))


def selected(order=None):
    binding = order.__dict__.get("shop_payment_intent") if order else None
    profile = settings.shop_monobank_profile
    if ((order and order.__dict__.get("shop_mono_checkbox_receipt"))
            or (binding and binding.profile.get("external_fiscalization_disabled") is False)):
        return True
    if internal_facts(order):
        return False
    if settings.shop_online_payment_enabled:
        # Online checkout selects the external issuer per order. Existing COD
        # and internal fiscal facts retain their own issuer and policy.
        return bool(order and order.payment_method == "onlineCard")
    return bool(settings.shop_mono_checkbox_enabled
        or (isinstance(profile, dict) and profile.get("external_fiscalization_disabled") is False))


def has_direct_facts(order):
    """Project persisted order facts independently of current global configuration."""
    binding = order.__dict__.get("shop_payment_intent")
    return bool(order.__dict__.get("shop_mono_checkbox_receipt")
        or (binding and binding.profile.get("external_fiscalization_disabled") is False))


def configuration_reasons(order=None):
    reasons = []
    profile = settings.shop_monobank_profile
    if not settings.shop_mono_checkbox_enabled or not settings.shop_payments_enabled or not settings.shop_order_operations_enabled:
        reasons.append("mono_checkbox_disabled")
    if not isinstance(profile, dict) or profile.get("external_fiscalization_disabled") is not False:
        reasons.append("mono_checkbox_profile_conflict")
    if (not settings.shop_online_payment_enabled
            and (settings.shop_checkbox_enabled or settings.shop_checkbox_profile is not None or settings.shop_fiscal_mock_enabled)):
        reasons.append("mono_checkbox_fiscal_mode_conflict")
    if order:
        binding = order.__dict__.get("shop_payment_intent")
        if binding and (binding.mode != "prepayment" or binding.profile.get("external_fiscalization_disabled") is not False
                or binding.profile != profile or binding.profile_hash != digest(profile)):
            reasons.append("mono_checkbox_profile_conflict")
        if any(order.__dict__.get(name) for name in ("shop_checkbox_binding", "shop_checkbox_documents",
                "shop_fiscal_documents", "shop_financial_returns", "shop_carrier_returns", "shop_checkbox_returns")):
            reasons.append("mono_checkbox_fiscal_mode_conflict")
        if sales.is_cod_order(order):
            reasons.append("mono_checkbox_prepayment_required")
    return list(dict.fromkeys(reasons))


def deny_internal_issuer(order):
    if selected(order):
        sales.fail("mono_checkbox_external_issuer_required", "This order uses an externally issued receipt; internal fiscal issuance is forbidden")


def check_proof(proof, binding, invoice_id):
    if (not isinstance(proof, dict) or proof.get("proof_hash") != digest({k: v for k, v in proof.items() if k != "proof_hash"})
            or proof.get("verified") is not True or proof.get("verification_scope") != "provider_invoice_association"
            or proof.get("invoice_id") != invoice_id or proof.get("profile_id") != binding.profile_id
            or proof.get("merchant_id") != binding.profile.get("merchant_id")
            or proof.get("authority") != binding.authority or proof.get("transport") != binding.transport
            or not isinstance(proof.get("checks"), list)):
        return "mono_checkbox_proof_invalid", None
    checks = proof["checks"]
    if not checks:
        return "mono_checkbox_receipt_pending", None
    # A return or competing issuer cannot silently become a valid sale receipt.
    if any(not isinstance(c, dict) for c in checks):
        return "mono_checkbox_proof_invalid", None
    if any(c.get("type") == "return" for c in checks):
        return "mono_checkbox_return_reconciliation_required", None
    if len(checks) != 1 or checks[0].get("type") != "sale" or checks[0].get("fiscalizationSource") != "checkbox":
        return "mono_checkbox_receipt_conflict", None
    check = checks[0]
    try:
        if str(UUID(check.get("id"))) != check["id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return "mono_checkbox_proof_invalid", None
    if check.get("status") not in {"new", "process", "done", "failed"}:
        return "mono_checkbox_proof_invalid", None
    if check["status"] != "done":
        return "mono_checkbox_receipt_failed" if check["status"] == "failed" else "mono_checkbox_receipt_pending", None
    return "accepted", check


def invoice_valid(proof, binding, invoice_id):
    return bool(isinstance(proof, dict) and proof.get("verified") is True and proof.get("status") == "success"
        and proof.get("invoice_id") == invoice_id and proof.get("reference") == binding.reference
        and proof.get("profile_id") == binding.profile_id and proof.get("merchant_id") == binding.profile["merchant_id"]
        and proof.get("authority") == binding.authority and proof.get("transport") == binding.transport
        and type(proof.get("amount_minor")) is int and proof["amount_minor"] == binding.amount_minor
        and proof.get("currency") == "UAH" and proof.get("proof_hash") == digest({k:v for k,v in proof.items() if k != "proof_hash"}))


def receipt_snapshot(order, confirmation):
    binding, evidence = order.shop_payment_intent, order.shop_payment_evidence
    return dict(order_id=order.id, payment_confirmation_id=confirmation.id, payment_id=confirmation.payment_id,
        evidence_id=evidence.id, evidence_hash=evidence.proof_hash, profile_id=binding.profile_id,
        profile_hash=binding.profile_hash, merchant_id=binding.profile["merchant_id"],
        invoice_id=evidence.proof["invoice_id"], reference=binding.reference, amount_minor=binding.amount_minor,
        currency="UAH", authority=binding.authority, provenance="mono_checkbox_external",
        verification_scope="provider_invoice_association", structured_totals_verified=False, reports_imported=False)


def accepted_receipt(order):
    confirmation = sales.canonical_payment(order)
    binding = order.__dict__.get("shop_payment_intent")
    evidence = order.__dict__.get("shop_payment_evidence")
    receipt = order.__dict__.get("shop_mono_checkbox_receipt")
    observations = order.__dict__.get("shop_mono_checkbox_observations", [])
    if (not confirmation or not binding or binding.mode != "prepayment" or not evidence or not receipt
            or binding.profile.get("external_fiscalization_disabled") is not False or not observations):
        return None
    expected = receipt_snapshot(order, confirmation)
    status, check = check_proof(receipt.proof, binding, expected["invoice_id"])
    latest = observations[-1]
    observed = latest.proof if isinstance(latest.proof, dict) else {}
    observed_status, observed_check = check_proof(observed.get("fiscal"), binding, expected["invoice_id"])
    if (not invoice_valid(observed.get("payment"), binding, expected["invoice_id"])
            or status != "accepted" or observed_status != "accepted" or latest.status != "accepted"
            or latest.proof_hash != digest(latest.proof) or observed_check["id"] != receipt.receipt_id
            or receipt.snapshot != expected or receipt.snapshot_hash != digest(expected)
            or receipt.proof_hash != digest(receipt.proof) or receipt.proof_canonical != payments.canonical(receipt.proof)
            or receipt.order_id != order.id or receipt.payment_confirmation_id != confirmation.id
            or receipt.evidence_id != evidence.id or receipt.profile_id != binding.profile_id
            or receipt.profile_hash != binding.profile_hash or receipt.authority != binding.authority
            or receipt.invoice_id != expected["invoice_id"] or check["id"] != receipt.receipt_id):
        return None
    return receipt


def projection(order):
    if not has_direct_facts(order):
        return None
    prefix = "simulated_mono_checkbox_external_" if sales.r2_test_context(order) else "mono_checkbox_external_"
    if accepted_receipt(order):
        return prefix + "accepted"
    observations = order.__dict__.get("shop_mono_checkbox_observations", [])
    status = observations[-1].status if observations else "receipt_pending"
    return prefix + ("invalid" if status == "accepted" else status.removeprefix("mono_checkbox_"))


class DirectFiscalPolicy:
    def __init__(self, order=None):
        binding = order.__dict__.get("shop_payment_intent") if order else None
        provider = payments.gateway()
        self.test_only = binding.authority == "test" if binding else provider.test_only
        self.available = not configuration_reasons(order) and provider.available and provider.external_checkbox

    def payment_methods(self, order):
        reasons = configuration_reasons(order)
        return ([] if reasons else ["card"]), reasons

    async def money_readiness(self, session, order):
        return configuration_reasons(order)

    async def readiness(self, session, order, phase):
        reasons = configuration_reasons(order)
        if not sales.canonical_payment(order):
            reasons.append("mono_checkbox_canonical_payment_required")
        elif not accepted_receipt(order):
            observations = order.__dict__.get("shop_mono_checkbox_observations", [])
            reasons.append(observations[-1].status if observations else "mono_checkbox_receipt_pending")
            if reasons[-1] == "accepted":
                reasons[-1] = "mono_checkbox_proof_invalid"
        return list(dict.fromkeys(reasons))

    async def authorize_in_session(self, session, **expected):
        order = await sales.load_order(session, expected["order_id"])
        await guard(session, order)
        reasons = await self.readiness(session, order, expected["phase"])
        if reasons:
            sales.fail(reasons[0], "Canonical payment and accepted external receipt are required")
        confirmation, receipt = sales.canonical_payment(order), accepted_receipt(order)
        if expected["payment_id"] != confirmation.payment_id:
            sales.fail("mono_checkbox_proof_invalid", "Fiscal authorization must bind the exact payment")
        return sales.FiscalAuthorization(**expected,
            verification_id=f"mono-checkbox:{receipt.receipt_id}:{receipt.snapshot_hash}",
            authority="test" if self.test_only else "production")


async def guard(session, order):
    reasons = configuration_reasons(order)
    if reasons:
        sales.fail(reasons[0], "Direct mono→Checkbox fiscal mode is unavailable")
    provider = payments.gateway()
    await payments.guard(session, provider)
    if not provider.external_checkbox:
        sales.fail("mono_checkbox_profile_conflict", "External Checkbox mode is required")
    binding = order.__dict__.get("shop_payment_intent")
    if binding:
        payments.pinned(binding, provider)
    return provider


async def view(session, actor, order):
    receipt = accepted_receipt(order)
    observations = order.__dict__.get("shop_mono_checkbox_observations", [])
    reasons = await DirectFiscalPolicy(order).readiness(session, order, "complete")
    confirmation = sales.canonical_payment(order)
    status = "accepted" if receipt else observations[-1].status if observations else "mono_checkbox_receipt_pending"
    if status == "accepted" and not receipt:
        status = "mono_checkbox_proof_invalid"
    check = receipt.proof["checks"][0] if receipt else None
    return DirectReceiptState(order_id=order.id, revision=order.revision,
        enabled=not configuration_reasons(order), paid=bool(confirmation), accepted=bool(receipt), status=status,
        authority=receipt.authority if receipt else "none", blocking_reasons=reasons,
        receipt=dict(id=receipt.id, receipt_id=receipt.receipt_id, invoice_id=receipt.invoice_id,
            profile_id=receipt.profile_id, profile_hash=receipt.profile_hash, source="checkbox",
            provenance="mono_checkbox_external", tax_url=check.get("taxUrl"), pdf_available=bool(receipt_file(receipt)),
            pdf_path=f"/api/v1/backoffice/shop-payments/orders/{order.id}/external-receipt/pdf" if receipt_file(receipt) else None,
            proof_hash=receipt.proof_hash, snapshot_hash=receipt.snapshot_hash,
            payment_evidence_id=receipt.evidence_id, imported_at=receipt.created_at.isoformat()) if receipt else None)


async def read(session, actor, order_id):
    order = await sales.load_order(session, order_id)
    actor = await sales.current_actor(session, actor, "read", order)
    result = await view(session, actor, order)
    session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id, operation="mono_checkbox_read",
        request_key="mono-checkbox-read:" + str(uuid4()), facts={"status":result.status}, created_at=datetime.now(UTC)))
    await session.commit()
    return result


async def refresh(session, actor, order_id, payload, raw_key):
    from app.services.monobank_provider import MonobankError
    actor_id = actor.id
    key = operation_key(raw_key, f"shop-mono-checkbox:{actor_id}:{order_id}")
    request_hash = digest(payload.model_dump(mode="json"))
    try:
        order = await sales.load_order(session, order_id)
        actor = await sales.current_actor(session, actor, "read", order)
        previous = await session.get(OrderIntent, key)
        if previous:
            if previous.payload_hash != request_hash:
                sales.fail("idempotency_conflict", "Key already belongs to another receipt request")
            if previous.response_json:
                if sales.r2_test_context(order):
                    await sales.require_test_database(session, "mono_checkbox_test_unsafe")
                return DirectReceiptState.model_validate(previous.response_json)
        provider = await guard(session, order)
        binding = order.__dict__.get("shop_payment_intent")
        confirmation = sales.canonical_payment(order)
        if not binding or not confirmation:
            sales.fail("mono_checkbox_canonical_payment_required", "Verify the exact paid invoice through R2 first")
        if (payload.expected_revision != order.revision or payload.expected_profile_id != binding.profile_id
                or payload.expected_profile_hash != binding.profile_hash):
            sales.fail("mono_checkbox_binding_conflict", "Reload the pinned payment profile and order revision")
        invoice_id = order.shop_payment_evidence.proof["invoice_id"]
        expected_reference, expected_amount = binding.reference, binding.amount_minor
        await session.rollback()  # provider reads never hold business locks
        payment_proof = None
        try:
            payment_proof = await provider.get_invoice(invoice_id, reference=expected_reference, amount_minor=expected_amount)
            proof = await provider.get_fiscal_checks(invoice_id) if payment_proof["status"] == "success" else None
            status = "mono_checkbox_payment_reconciliation_required"
        except MonobankError as error:
            error_reporting.provider_failure("receipt_verify", error.code, order_id=order_id)
            proof, status = None, error.code
        order = await sales.load_order(session, order_id, lock=True)
        actor = await sales.current_actor(session, SimpleNamespace(id=actor_id), "read", order)
        command = await claim_intent(session, key, request_hash)
        if command.response_json:
            return DirectReceiptState.model_validate(command.response_json)
        await guard(session, order)
        binding = order.shop_payment_intent
        confirmation = sales.canonical_payment(order)
        if not confirmation or order.revision != payload.expected_revision:
            sales.fail("revision_conflict", "Reload the paid order before receipt recovery")
        if proof is not None and invoice_valid(payment_proof, binding, invoice_id):
            status, check = check_proof(proof, binding, invoice_id)
        else:
            check = None
        if status == "accepted":
            receipt = order.__dict__.get("shop_mono_checkbox_receipt")
            if receipt and receipt.receipt_id != check["id"]:
                status = "mono_checkbox_receipt_conflict"
            elif not receipt:
                snapshot = receipt_snapshot(order, confirmation)
                receipt = Receipt(order_id=order.id, payment_confirmation_id=confirmation.id,
                    evidence_id=order.shop_payment_evidence.id, profile_id=binding.profile_id, profile_hash=binding.profile_hash,
                    invoice_id=invoice_id, receipt_id=check["id"], authority=binding.authority,
                    snapshot=snapshot, snapshot_hash=digest(snapshot), proof=proof, proof_canonical=payments.canonical(proof),
                    proof_hash=digest(proof), operator_id=actor.id, created_at=datetime.now(UTC))
                session.add(receipt)
                await session.flush()
            if status == "accepted" and check.get("file") and not receipt.proof["checks"][0].get("file"):
                artifact = await session.scalar(select(Artifact).where(Artifact.receipt_id == receipt.id))
                if not artifact:
                    session.add(Artifact(receipt_id=receipt.id, proof=proof, proof_hash=digest(proof),
                        operator_id=actor.id, created_at=datetime.now(UTC)))
        observation_proof = deepcopy(proof)
        if observation_proof:
            # Preserve the source digest while keeping repeated PDF bytes out of
            # eagerly loaded observation history. Receipt/artifact owns the PDF.
            observation_proof["upstream_proof_hash"] = observation_proof["proof_hash"]
            for observed_check in observation_proof["checks"]:
                if observed_check.get("file"):
                    observed_check["file_sha256"] = sha256(observed_check.pop("file").encode()).hexdigest()
            observation_proof["proof_hash"] = digest({k:v for k,v in observation_proof.items() if k != "proof_hash"})
        observation = {"fiscal":observation_proof, "payment":payment_proof}
        session.add(Observation(order_id=order.id, status=status, proof=observation, proof_hash=digest(observation),
            operator_id=actor.id, created_at=datetime.now(UTC)))
        order.revision += 1
        session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id, operation="mono_checkbox_refresh",
            request_key=key, facts={"status":status,"invoice_id":invoice_id}, created_at=datetime.now(UTC)))
        await session.flush()
        order = await sales.load_order(session, order_id)
        result = await view(session, actor, order)
        command.order_id, command.response_json = order.id, result.model_dump(mode="json")
        await guard(session, order)
        await session.commit()
        return result
    except IntegrityError:
        await session.rollback()
        sales.fail("mono_checkbox_receipt_conflict", "Receipt identity is already imported; reload the canonical order")
    except Exception as error:
        error_reporting.report_exception("receipt_verify", error, order_id=order_id)
        await session.rollback()
        raise


async def download_pdf(session, actor, order_id):
    import base64
    order = await sales.load_order(session, order_id)
    actor = await sales.current_actor(session, actor, "read", order)
    if sales.r2_test_context(order):
        await sales.require_test_database(session, "mono_checkbox_test_unsafe")
    receipt = accepted_receipt(order)
    file = receipt_file(receipt)
    if not file:
        sales.fail("mono_checkbox_pdf_unavailable", "No accepted external receipt PDF is available", 404)
    content = base64.b64decode(file, validate=True)
    if len(content) > 1_000_000 or not content.startswith(b"%PDF-"):
        sales.fail("mono_checkbox_proof_invalid", "The stored receipt PDF is invalid")
    session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id, operation="mono_checkbox_pdf_read",
        request_key="mono-checkbox-pdf:" + str(uuid4()), facts={"receipt_id":receipt.receipt_id}, created_at=datetime.now(UTC)))
    await session.commit()
    return receipt.receipt_id, content


def receipt_file(receipt):
    if not receipt:
        return None
    original = receipt.proof["checks"][0].get("file")
    if original:
        return original
    artifact = receipt.__dict__.get("artifact")
    if artifact and artifact.receipt_id == receipt.id and artifact.proof_hash == digest(artifact.proof):
        binding = SimpleNamespace(profile_id=receipt.profile_id, profile={"merchant_id":receipt.snapshot["merchant_id"]},
            authority=receipt.authority, transport=receipt.proof["transport"])
        status, check = check_proof(artifact.proof, binding, receipt.invoice_id)
        if status == "accepted" and check["id"] == receipt.receipt_id:
            return check.get("file")
    return None
