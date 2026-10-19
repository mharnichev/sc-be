"""Read-only Monobank verification and explicit, structured COD settlement.

An intent is a server binding, never a request to create or charge an invoice.
The operator prepares the invoice externally using its exact reference.
"""
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, DBAPIError

from app.core.config import settings
from app.models.order import OrderIntent, OrderPayment, OrderStatus
from app.models.shop_payments import ShopPaymentIntent as Intent, ShopPaymentEvidence as Evidence
from app.models.shop_sales import ShopPaymentConfirmation, ShopOperationAudit
from app.schemas.shop_payments import PaymentReadiness, PaymentState
from app.services import shop_sales as sales
from app.services.order_documents import claim_intent, digest, minor, operation_key


def gateway():
    from app.services.monobank_provider import MonobankGateway
    return MonobankGateway()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manual_policy():
    policy = settings.shop_payment_manual_policy
    if (not isinstance(policy, dict) or set(policy) != {"id", "issuer", "account", "types", "environment"}
            or any(not isinstance(policy.get(k), str) or not policy[k].strip() or policy[k] != policy[k].strip()
                   or len(policy[k]) > (64 if k == "id" else 100) for k in ("id", "issuer", "account"))
            or policy.get("environment") not in {"test", "production"} or not isinstance(policy.get("types"), list)
            or any(not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}",policy.get(k,"")) for k in ("issuer","account"))
            or not policy["types"] or any(t not in {"bank_statement", "carrier_settlement"} for t in policy["types"])):
        return None
    return policy


async def selected(session, order_id):
    return settings.shop_payments_enabled or bool(await session.scalar(select(Intent.id).where(Intent.order_id == order_id)))


async def guard(session, provider=None):
    if not settings.shop_order_operations_enabled or not settings.shop_payments_enabled:
        sales.fail("shop_payments_disabled", "Shop payment operations are closed", 503)
    if provider:
        if not provider.available:
            sales.fail("monobank_unavailable", "Monobank verification is not configured", 503)
        if provider.test_only:
            await sales.require_test_database(session, "monobank_test_unsafe")


async def manual_guard(session):
    if not manual_policy():
        sales.fail("manual_cod_policy_unavailable", "Configure the isolated COD settlement account policy")
    if manual_policy()["environment"] == "test":
        await sales.require_test_database(session, "manual_cod_test_unsafe")


async def readiness(session, actor):
    await sales.current_actor(session, actor, "read", SimpleNamespace(fulfillment_method=None))
    provider = gateway()
    reasons = []
    enabled = settings.shop_payments_enabled and settings.shop_order_operations_enabled
    if not enabled:
        reasons.append("shop_payments_disabled")
    safe = not provider.test_only or await sales.test_database_is_safe(session)
    available = bool(enabled and provider.available and safe)
    if not provider.available:
        reasons.append("monobank_unavailable")
    if not safe:
        reasons.append("monobank_test_unsafe")
    return PaymentReadiness(enabled=enabled, provider_available=available,
        profile_id=provider.profile["id"] if provider.profile else None,
        profile_hash=digest(provider.profile) if provider.profile else None,
        authority=("test" if provider.test_only else "provider") if available else "none",
        transport=("mock_http" if provider.test_only else "https") if available else "unavailable",
        blocking_reasons=reasons,manual_policy=manual_policy(),manual_policy_hash=digest(manual_policy()) if manual_policy() else None,
        replay_actions={"invoice_intent":True,"verify":True,"manual_cod":True})


async def read(session, actor, order_id, *, verification_status=None):
    order = await sales.load_order(session, order_id)
    actor = await sales.current_actor(session, actor, "read", order)
    ready = await readiness(session, actor)
    binding = await session.scalar(select(Intent).where(Intent.order_id == order_id))
    from app.services.shop_payment_resolutions import effective_invoice,payment_blocked
    effective,correction=await effective_invoice(session,binding) if binding else (None,None)
    cod = sales.is_cod_order(order)
    payment = sales.canonical_payment(order)
    evidence = order.__dict__.get("shop_payment_evidence") if payment else None
    base = await sales.response(session, order, actor)
    reasons = [r for r in ready.blocking_reasons if not cod or r not in {"monobank_unavailable", "monobank_test_unsafe"}]
    if cod:
        if not manual_policy():
            reasons.append("manual_cod_policy_unavailable")
        if manual_policy() and manual_policy()["environment"] == "test" and not await sales.test_database_is_safe(session):
            reasons.append("manual_cod_test_unsafe")
        if binding and (not manual_policy() or binding.profile_hash != digest(manual_policy())):
            reasons.append("manual_cod_profile_mismatch")
    if not sales.eligible_order(order):
        reasons.append("shop_order_required")
    if order.payments and not payment or order.qr_payment_attempts:
        reasons.append("incompatible_sales_facts")
    if binding and binding.mode == "prepayment" and (not gateway().profile or binding.profile_hash != digest(gateway().profile)):
        reasons.append("monobank_profile_mismatch")
    from app.services.shop_carrier_returns import active_case
    from app.services.shop_np_returns import active_case as np_active
    if active_case(order) or np_active(order):
        reasons.append("carrier_return_active")
    if payment_blocked(order):
        reasons.append("resolution_active")
    if minor(order.total_amount) <= 0:
        reasons.append("unsupported_zero_total")
    policy = sales.effective_fiscal_policy(order)
    if hasattr(policy, "money_readiness") and not payment:
        reasons.extend(await policy.money_readiness(session, order))
    if not payment and cod:
        try:
            shipment = await sales.real_shipment(session, order, delivered=True)
            fulfillment = sales.canonical_fulfillment(order)
            if not fulfillment or fulfillment.shipment_id != shipment.id:
                reasons.append("handover_required")
        except HTTPException as error:
            reasons.append(error.detail["code"])
    enabled = ready.enabled and sales.eligible_order(order) and not reasons and not order.payments and order.status not in {OrderStatus.cancelled, OrderStatus.completed}
    profile = manual_policy() if cod else gateway().profile
    return PaymentState(enabled=bool(enabled), profile_id=binding.profile_id if binding else profile["id"] if profile else None,
        test_context=bool(binding.authority == "test" or binding.profile.get("environment") == "test") if binding else bool(profile and (profile.get("environment") == "test" or not cod and gateway().test_only)),
        profile_hash=binding.profile_hash if binding else digest(profile) if profile else None,
        authority=payment.authority if payment else ready.authority if not cod else "none",
        order_id=order.id, revision=order.revision, mode="cod" if cod else "prepayment",
        intent={"id": binding.id, "reference": binding.reference, "amount_minor": binding.amount_minor,
            "invoice_id": binding.invoice_id,"effective_invoice_id":effective,"correction_id":correction.id if correction else None, "status": "accepted" if payment else "awaiting_verification" if binding.invoice_id else "awaiting_invoice"} if binding else None,
        payment=base.payment, evidence={"id":evidence.id,"kind":binding.mode,"authority":evidence.authority,
            "test_context":sales.r2_test_context(order),"operator_id":evidence.operator_id,"created_at":evidence.created_at.isoformat(),
            "proof_hash":evidence.proof_hash,"proof":evidence.proof} if evidence else None,
        actions={"create_intent": bool(enabled and not cod and not binding and ready.provider_available),
            "verify_invoice": bool(enabled and not cod and binding and ready.provider_available),
            "record_manual_cod": bool(enabled and cod)}, blocking_reasons=list(dict.fromkeys(reasons)), readiness=ready,
        verification_status=verification_status)


def validate_order(order, revision):
    from app.services.shop_payment_resolutions import payment_blocked
    if payment_blocked(order):
        sales.fail("resolution_active", "Use the existing immutable payment resolution")
    if not sales.eligible_order(order):
        sales.fail("shop_order_required", "A non-simulated Shop order is required")
    if minor(order.total_amount) <= 0:
        sales.fail("unsupported_zero_total", "Payment requires a positive full order total")
    if order.revision != revision:
        sales.fail("revision_conflict", "Reload order")
    if order.status in {OrderStatus.cancelled, OrderStatus.completed}:
        sales.fail("terminal_order", "Order is closed")
    from app.services.shop_carrier_returns import active_case
    from app.services.shop_np_returns import active_case as np_active
    if active_case(order) or np_active(order):
        sales.fail("carrier_return_active", "Use the canonical carrier return workflow")
    if order.payments or order.qr_payment_attempts or (order.handovers and not sales.canonical_fulfillment(order)):
        sales.fail("incompatible_sales_facts", "Existing money or incompatible sales facts prevent new payment")


def pinned(binding, provider):
    if (not provider.profile or binding.profile_hash != digest(provider.profile) or binding.profile != provider.profile
            or binding.authority != ("test" if provider.test_only else "provider")
            or binding.transport != ("mock_http" if provider.test_only else "https")):
        sales.fail("monobank_profile_mismatch", "The payment intent belongs to another configured profile")


async def admit(session, order, method):
    from app.services.shop_checkbox import payment_barrier
    from app.services.shop_fiscal import payment_barrier as local_barrier
    await payment_barrier(session, order, method, minor(order.total_amount))
    await local_barrier(session)


async def record(session, order, actor, binding, proof, *, authority, source, reference, identity, method, correction_id=None):
    now = datetime.now(UTC)
    await admit(session, order, method)
    namespace = "shop_r2_cod" if source == "manual" else "shop_r2_monobank:" + binding.profile_id
    payment = OrderPayment(order_id=order.id, amount_minor=minor(order.total_amount), method=method,
        source="shop_" + source, evidence=reference, operator_id=actor.id, simulation=False, created_at=now)
    session.add(payment)
    await session.flush()
    evidence = Evidence(correction_id=correction_id,intent_id=binding.id, order_id=order.id, payment_id=payment.id, identity=identity,
        authority=authority, proof=proof, proof_canonical=canonical(proof), proof_hash=digest(proof), operator_id=actor.id, created_at=now)
    session.add(evidence)
    await session.flush()
    snapshot = dict(order_id=order.id,payment_id=payment.id,amount_minor=payment.amount_minor,currency="UAH",method=method,
        namespace=namespace,external_reference=reference,source=source,authority=authority,
        verification={"r2_evidence_id":evidence.id,"intent_id":binding.id,"proof_hash":evidence.proof_hash},original_payment_method=order.payment_method)
    session.add(ShopPaymentConfirmation(order_id=order.id,payment_id=payment.id,namespace=namespace,external_reference=reference,
        source=source,authority=authority,snapshot=snapshot,snapshot_hash=digest(snapshot),operator_id=actor.id,created_at=now))
    order.payment_method, order.status = method, OrderStatus.paid
    order.revision += 1


async def command(session, actor, order_id, payload, raw_key, operation):
    from app.services.monobank_provider import MonobankError
    actor_id = actor.id
    key = operation_key(raw_key, f"shop-r2-{operation}:{actor_id}:{order_id}")
    request_hash = digest(payload.model_dump(mode="json"))
    try:
        order = await sales.load_order(session, order_id)
        actor = await sales.current_actor(session, actor, "read", order)
        existing = await session.scalar(select(Intent).where(Intent.order_id == order_id))
        previous = await session.get(OrderIntent, key)
        if previous:
            if previous.payload_hash != request_hash:
                sales.fail("idempotency_conflict", "Key already used for another request")
            if previous.response_json:
                if existing and (existing.authority == "test" or existing.profile.get("environment") == "test"):
                    await sales.require_test_database(session, "payment_test_unsafe")
                return PaymentState.model_validate(previous.response_json)
        await guard(session)
        if existing:
            if existing.mode == "prepayment":
                await guard(session, gateway())
                pinned(existing, gateway())
            else:
                await manual_guard(session)
                if existing.profile_hash != digest(manual_policy()):
                    sales.fail("manual_cod_profile_mismatch", "Settlement policy differs from the stored binding")
        order = await sales.load_order(session, order_id, lock=True)
        command_intent = await claim_intent(session, key, request_hash)
        if command_intent.response_json:
            return PaymentState.model_validate(command_intent.response_json)
        validate_order(order, payload.expected_revision)
        binding = await session.scalar(select(Intent).where(Intent.order_id == order_id).with_for_update())
        provider = gateway()
        asserted_profile = manual_policy() if operation == "manual_cod" else provider.profile
        if (not asserted_profile or payload.expected_profile_id != asserted_profile["id"]
                or payload.expected_profile_hash != digest(asserted_profile)):
            sales.fail("payment_profile_conflict", "Reload the current server payment profile")
        if operation in {"invoice_intent", "verify"}:
            if sales.is_cod_order(order):
                sales.fail("cod_settlement_required", "COD uses the separate structured settlement workflow")
            await guard(session, provider)
            if operation == "invoice_intent":
                if binding:
                    sales.fail("payment_intent_exists", "An immutable payment intent already exists")
                # R1 admission happens before the operator can prepare this invoice.
                await admit(session, order, "card")
                binding = Intent(id=str(uuid4()), order_id=order.id, mode="prepayment", reference=str(uuid4()),
                    amount_minor=minor(order.total_amount), profile_id=provider.profile["id"], profile=provider.profile,
                    profile_hash=digest(provider.profile), authority="test" if provider.test_only else "provider",
                    transport="mock_http" if provider.test_only else "https",operator_id=actor.id, created_at=datetime.now(UTC))
                session.add(binding)
                order.revision += 1
            else:
                if not binding or binding.mode != "prepayment":
                    sales.fail("payment_intent_required", "Prepare the server payment intent first")
                pinned(binding, provider)
                from app.services.shop_payment_resolutions import effective_invoice
                effective,correction=await effective_invoice(session,binding)
                if effective and effective != payload.invoice_id:
                    sales.fail("invoice_binding_conflict", "Retry the original invoice candidate")
                if not binding.invoice_id:
                    binding.invoice_id = payload.invoice_id
                expected_reference, expected_amount = binding.reference, binding.amount_minor
                await session.flush()
                # The first candidate survives unknown/pending responses. No business
                # row lock spans the provider GET, and retries cannot switch invoices.
                await session.commit()
                await guard(session, provider)
                await session.rollback()
                try:
                    proof = await provider.get_invoice(payload.invoice_id, reference=expected_reference, amount_minor=expected_amount)
                    status = proof["status"]
                except MonobankError as error:
                    proof, status = None, error.code
                actor = await sales.current_actor(session, SimpleNamespace(id=actor_id), "read", await sales.load_order(session, order_id))
                order = await sales.load_order(session, order_id, lock=True)
                await guard(session, gateway())
                binding = await session.scalar(select(Intent).where(Intent.order_id == order_id).with_for_update().execution_options(populate_existing=True))
                pinned(binding, gateway())
                command_intent = await claim_intent(session, key, request_hash)
                if command_intent.response_json:
                    return PaymentState.model_validate(command_intent.response_json)
                validate_order(order, payload.expected_revision)
                effective,correction=await effective_invoice(session,binding)
                if proof and status == "success":
                    try:
                        accepted_at = datetime.fromisoformat(proof.get("accepted_at", ""))
                        created_at = datetime.fromisoformat(proof.get("created_at", ""))
                        dated = (accepted_at.tzinfo is not None and created_at.tzinfo is not None
                            and created_at >= sales.aware(binding.created_at) - timedelta(seconds=30)
                            and created_at <= accepted_at <= datetime.now(UTC)+timedelta(seconds=30))
                    except (ValueError, TypeError):
                        dated = False
                    if (proof.get("verified") is not True or proof.get("invoice_id") != effective
                            or proof.get("reference") != binding.reference or type(proof.get("amount_minor")) is not int
                            or proof["amount_minor"] != binding.amount_minor or proof.get("currency") != "UAH"
                            or proof.get("profile_id") != binding.profile_id or proof.get("merchant_id") != binding.profile["merchant_id"]
                            or proof.get("authority") != ("test" if provider.test_only else "provider") or not dated
                            or proof.get("transport") != ("mock_http" if provider.test_only else "https")
                            or proof.get("proof_hash") != digest({k:v for k,v in proof.items() if k != "proof_hash"})):
                        sales.fail("monobank_proof_invalid", "Invoice proof does not match the pinned payment")
                    await record(session, order, actor, binding, proof, authority=proof["authority"],source="provider",
                        reference=effective, identity=digest([binding.profile_id,effective]),method="card",correction_id=correction.id if correction else None)
                result = await read(session, actor, order_id, verification_status=status)
                session.add(ShopOperationAudit(order_id=order.id,operator_id=actor.id,operation="r2_verify",request_key=key,
                    facts={"intent_id":binding.id,"status":status,"revision":order.revision},created_at=datetime.now(UTC)))
                command_intent.response_json = result.model_dump(mode="json")
                await guard(session, gateway())
                pinned(binding, gateway())
                await session.commit()
                return result
        elif operation == "manual_cod":
            await manual_guard(session)
            if not sales.is_cod_order(order):
                sales.fail("cod_order_required", "Structured COD settlement requires a COD order")
            evidence = payload.evidence
            profile = manual_policy()
            if (evidence.issuer != profile["issuer"] or evidence.account != profile["account"]
                    or evidence.type not in profile["types"]):
                sales.fail("manual_cod_profile_mismatch", "Evidence must match the configured issuer, account and document type")
            if evidence.gross_amount_minor != minor(order.total_amount):
                sales.fail("amount_mismatch", "COD gross settlement must equal the full order total")
            if evidence.settled_at > datetime.now(UTC) + timedelta(seconds=30):
                sales.fail("settlement_date_invalid", "Settlement timestamp cannot be in the future")
            shipment = await sales.real_shipment(session, order, delivered=True, lock=True)
            fulfillment = sales.canonical_fulfillment(order)
            if not fulfillment or fulfillment.shipment_id != shipment.id:
                sales.fail("handover_required", "COD settlement requires the exact canonical carrier dispatch")
            if shipment.tracking_number != evidence.tracking_number:
                sales.fail("settlement_shipment_mismatch", "Settlement must identify the dispatched tracking number")
            if evidence.settled_at < sales.aware(shipment.provider_event_at):
                sales.fail("settlement_date_invalid", "COD settlement cannot precede the accepted delivery event")
            if binding:
                sales.fail("payment_intent_exists", "A payment binding already exists")
            binding = Intent(id=str(uuid4()),order_id=order.id,mode="cod",reference=str(uuid4()),amount_minor=minor(order.total_amount),
                profile_id=profile["id"],profile=profile,profile_hash=digest(profile),authority="manual",transport="manual",
                operator_id=actor.id,created_at=datetime.now(UTC))
            session.add(binding)
            await session.flush()
            proof = evidence.model_dump(mode="json")
            identity = digest([evidence.issuer.casefold(),evidence.account.casefold(),evidence.document_id.casefold(),evidence.line_id.casefold()])
            await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("shop-r2-evidence:"+identity,0))))
            if await session.scalar(select(Evidence.id).where(Evidence.identity == identity)):
                sales.fail("settlement_reference_used", "This settlement line already belongs to an order")
            await record(session,order,actor,binding,proof,authority="manual",source="manual",reference=identity,identity=identity,method="cod")
        else:
            raise ValueError("Unsupported payment operation")
        session.add(ShopOperationAudit(order_id=order.id,operator_id=actor.id,operation="r2_"+operation,request_key=key,
            facts={"intent_id":binding.id,"revision":order.revision},created_at=datetime.now(UTC)))
        await session.flush()
        result = await read(session,actor,order_id)
        command_intent.response_json = result.model_dump(mode="json")
        await guard(session,provider if operation == "invoice_intent" else None)
        if operation == "invoice_intent":
            pinned(binding, gateway())
        if operation == "manual_cod":
            await manual_guard(session)
            if binding.profile_hash != digest(manual_policy()):
                sales.fail("manual_cod_profile_mismatch", "Settlement policy changed during admission")
        await session.commit()
        return result
    except IntegrityError:
        await session.rollback()
        sales.fail("payment_binding_conflict", "Invoice or settlement identity already belongs to another payment")
    except DBAPIError as error:
        await session.rollback()
        if "Invoice identity belongs" in str(error.orig):
            sales.fail("payment_binding_conflict","Invoice already belongs to another immutable payment")
        raise
    except Exception:
        await session.rollback()
        raise
