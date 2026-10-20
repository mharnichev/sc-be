"""Durable live POS invoice dispatch and GET-only payment/receipt reconciliation."""
import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.master_mono import MasterMonoBinding as Binding, MasterMonoPayment as Payment, MasterMonoReceipt as Receipt, MasterMonoObservation as Observation, MasterMonoArtifact as Artifact, MasterMonoCreateRequest as CreateRequest
from app.models.order import OrderIntent, OrderPayment, OrderStatus
from app.schemas.master_mono import MasterMonoState
from app.services import local_sales, sales_access, shop_mono_checkbox as direct
from app.services.order_documents import claim_intent, digest, minor, operation_key


def fail(code, message, status=409):
    raise HTTPException(status, {"code": code, "message": message})


async def actor_order(session, actor, order_id, *, lock=False, manager=False):
    actor = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id).with_for_update(read=True).execution_options(populate_existing=True))
    if not actor or not actor.is_active or not (actor.is_superuser or (actor.can_manage_fiscal_settings if manager else actor.can_record_sales and actor.can_accept_qr_payments)):
        raise HTTPException(403, "Sales and QR payment permissions required")
    order = await local_sales.load(session, order_id, lock=lock)
    if manager:
        await sales_access.deny_master(session, actor)
    else:
        await sales_access.own_sale(session, actor, order)
    if order.channel != "backoffice_pos" or (not manager and order.created_by_user_id != actor.id):
        raise HTTPException(404, "Order not found")
    if not order.simulation and gateway().test_only:
        await direct.sales.require_test_database(session, "master_mono_test_unsafe")
    return actor, order


class MasterBankConfig:
    """Map current master settings without selecting a fiscal strategy for Shop."""
    shop_mono_checkbox_enabled = True

    def __getattr__(self, name):
        if name == "shop_monobank_token":
            token = settings.master_monobank_token
            return token if token is not None and token.get_secret_value() else settings.shop_monobank_token
        source = {
            "shop_monobank_profile": "master_monobank_profile",
            "shop_monobank_live_transport_enabled": "master_monobank_live_transport_enabled",
            "master_mono_enabled": "master_mono_enabled",
            "master_mono_invoice_create_enabled": "master_mono_invoice_create_enabled",
            "master_mono_checkbox_link_confirmed": "master_mono_checkbox_link_confirmed",
            "app_env": "app_env",
        }.get(name)
        if source is None:
            raise AttributeError(name)
        return getattr(settings, source)


def gateway(binding=None, *, transport=None):
    """Keep the merchant private while rechecking live settings before bank I/O."""
    from app.services.monobank_provider import MonobankGateway
    return MonobankGateway(MasterBankConfig(), transport=transport)


def fiscal_policy():
    policy = settings.master_mono_fiscal_policy
    if (not isinstance(policy, dict) or set(policy) != {"id", "tax_codes", "unit"}
            or not isinstance(policy["id"], str) or not policy["id"].strip() or len(policy["id"]) > 64
            or policy["unit"] != "шт." or not isinstance(policy["tax_codes"], list)
            or not 1 <= len(policy["tax_codes"]) <= 10
            or any(type(c) is not int or not 0 <= c <= 99 for c in policy["tax_codes"])
            or len(set(policy["tax_codes"])) != len(policy["tax_codes"])):
        fail("master_mono_fiscal_policy_required", "Approve an explicit fiscal tax and unit policy before checkout")
    return deepcopy(policy)


def guard(session, binding=None, *, create=False):
    provider = gateway()
    if (not settings.master_mono_enabled or not settings.master_mono_checkbox_link_confirmed
            or not provider.available or not provider.external_checkbox
            or provider.profile.get("environment") != "production"
            or create and not settings.master_mono_invoice_create_enabled):
        fail("master_mono_disabled", "Enable and verify the production mono to Checkbox checkout configuration")
    if not provider.test_only and settings.app_env not in {"production", "staging"}:
        fail("master_mono_disabled", "HTTPS checkout requires a production or staging application environment")
    if provider.test_only and (type(provider.transport) is not httpx.MockTransport
            or not local_sales.test_database_url(session)):
        fail("master_mono_test_unsafe", "Injected provider transport requires an isolated test database")
    if binding and (binding.profile != provider.profile or binding.profile_hash != digest(provider.profile)
            or binding.authority != ("test" if provider.test_only else "provider")
            or binding.transport != ("mock_http" if provider.test_only else "https")):
        fail("master_mono_profile_conflict", "Reload the original pinned payment profile")
    fiscal_policy()
    return provider


def basket_for(order):
    policy = fiscal_policy()
    lines = sorted(order.items, key=lambda i: i.id)
    if not lines or any(type(i.quantity) is not int or i.quantity <= 0 or i.quantity_from_stock != i.quantity
            or i.quantity_to_order or i.quantity_received_for_order for i in lines):
        fail("master_mono_stock_required", "Only fully reserved goods without procurement can be charged")
    weights = [minor(i.total_price if i.total_price is not None else i.price * i.quantity) for i in lines]
    amount, gross = minor(order.total_amount), sum(weights)
    if amount <= 0 or gross < amount or any(w <= 0 for w in weights):
        fail("master_mono_amount_invalid", "The server basket must have a positive supported total")
    allocated = [amount * weight // gross for weight in weights]
    for index in sorted(range(len(lines)), key=lambda n: (-(amount * weights[n] % gross), lines[n].id))[:amount-sum(allocated)]:
        allocated[index] += 1
    basket = []
    for item, total in zip(lines, allocated):
        each, remainder = divmod(total, item.quantity)
        if each <= 0 or not item.product_name or len(item.product_name) > 128:
            fail("master_mono_basket_invalid", "Goods need supported names and positive unit prices")
        for quantity, price in ((item.quantity-remainder, each), (remainder, each+1)):
            if quantity:
                basket.append(dict(name=item.product_name, qty=quantity, sum=price, total=quantity*price,
                    code=str(item.product_id), unit=policy["unit"], tax=policy["tax_codes"]))
    if len(basket) > 200:
        fail("master_mono_basket_invalid", "The fiscal basket exceeds the supported item count")
    return basket, policy


def canonical_payment(order):
    binding = order.__dict__.get("master_mono_binding")
    evidence = order.__dict__.get("master_mono_payment")
    if not binding or not evidence or len(order.payments) != 1:
        return None
    payment = order.payments[0]
    if (order.channel != "backoffice_pos" or order.simulation or not binding.create_request_id or binding.order_id != order.id
            or binding.operator_id != order.created_by_user_id or binding.amount_minor != minor(order.total_amount)
            or binding.profile_hash != digest(binding.profile) or (binding.authority, binding.transport) not in {( "test", "mock_http"), ("provider", "https")}
            or payment.id != evidence.payment_id or evidence.binding_id != binding.id or evidence.order_id != order.id
            or payment.operator_id != binding.operator_id or payment.amount_minor != binding.amount_minor
            or payment.simulation or payment.source != "master_mono_provider" or payment.method != "card"
            or payment.evidence != binding.invoice_id or evidence.proof_hash != digest(evidence.proof)
            or not direct.invoice_valid(evidence.proof, binding, binding.invoice_id)):
        return None
    return payment


def dated_invoice(proof, binding):
    try:
        created = datetime.fromisoformat(proof["created_at"])
        accepted = datetime.fromisoformat(proof["accepted_at"])
        bound = binding.created_at.replace(tzinfo=UTC) if binding.created_at.tzinfo is None else binding.created_at
        return (created.tzinfo is not None and accepted.tzinfo is not None
            and bound - timedelta(seconds=30) <= created <= accepted <= datetime.now(UTC) + timedelta(seconds=30))
    except (KeyError, TypeError, ValueError):
        return False


def accepted_receipt(order):
    binding = order.__dict__.get("master_mono_binding")
    receipt = order.__dict__.get("master_mono_receipt")
    observations = order.__dict__.get("master_mono_observations", [])
    payment = canonical_payment(order)
    if not payment or not receipt or not observations:
        return None
    latest = observations[-1]
    if (receipt.order_id != order.id or receipt.binding_id != binding.id or receipt.payment_id != payment.id
            or receipt.proof_hash != digest(receipt.proof) or latest.status != "accepted"
            or latest.proof_hash != digest(latest.proof)
            or not direct.invoice_valid(latest.proof.get("payment"), binding, binding.invoice_id)
            or not dated_invoice(latest.proof.get("payment"), binding)):
        return None
    status, check = direct.check_proof(receipt.proof, binding, binding.invoice_id)
    observed_status, observed_check = direct.check_proof(latest.proof.get("fiscal"), binding, binding.invoice_id)
    if status != "accepted" or observed_status != "accepted" or check["id"] != receipt.receipt_id or observed_check["id"] != receipt.receipt_id:
        return None
    return receipt


def pdf_bytes(receipt):
    encoded = receipt.proof["checks"][0].get("file") if receipt else None
    artifact = receipt.__dict__.get("artifact") if receipt else None
    if not encoded and artifact and artifact.proof_hash == digest(artifact.proof):
        encoded = artifact.proof["checks"][0].get("file")
    if not encoded:
        return None
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    return content if len(content) <= 1_000_000 and content.startswith(b"%PDF-") else None


def view(session, order):
    binding = order.__dict__.get("master_mono_binding")
    request = order.__dict__.get("master_mono_create_request")
    observations = order.__dict__.get("master_mono_observations", [])
    receipt = accepted_receipt(order)
    paid = canonical_payment(order) is not None
    status = "accepted" if receipt else observations[-1].status if observations else "waiting" if binding else "creation_unknown" if request else "not_started"
    if status == "accepted" and not receipt:
        status = "master_mono_proof_invalid"
    reasons = []
    if order.simulation:
        reasons.append("legacy_simulation")
    try:
        guard(session, binding or request, create=not (binding or request))
    except HTTPException as error:
        reasons.append(error.detail["code"])
    enabled = not reasons
    if not receipt and (binding or request):
        reasons.append(status)
    if not (binding or request) and (order.payments or order.handovers or order.shipments or order.qr_payment_attempts
            or order.status in {OrderStatus.completed, OrderStatus.cancelled}):
        reasons.append("master_mono_checkout_owned")
        enabled = False
    fresh = bool(observations and (datetime.now(UTC) - observations[-1].created_at.replace(tzinfo=UTC)).total_seconds() <= 30)
    if receipt and not fresh:
        reasons.append("master_mono_observation_stale")
    return MasterMonoState(enabled=enabled, order_id=order.id, revision=order.revision, paid=paid,
        receipt_accepted=bool(receipt), status=status, amount_minor=minor(order.total_amount),
        invoice_id=binding.invoice_id if binding and not order.simulation else None,
        payment_url=binding.page_url if binding and not order.simulation else None,
        receipt_pdf_path=f"/api/v1/backoffice/master-mono/orders/{order.id}/receipt/pdf" if pdf_bytes(receipt) else None,
        can_handover=bool(receipt and not reasons and not order.handovers and order.status not in {OrderStatus.completed, OrderStatus.cancelled}),
        blocking_reasons=list(dict.fromkeys(reasons)))


async def read(session, actor, order_id):
    _, order = await actor_order(session, actor, order_id)
    result = view(session, order)
    if not order.__dict__.get("master_mono_binding"):
        from app.models.fiscal import FiscalDocument
        from app.services.fiscal_automation import owner
        if await owner(session,order.id) or await session.scalar(select(FiscalDocument.id).where(FiscalDocument.order_id==order.id)):
            result.enabled = False
            result.blocking_reasons.append("master_mono_issuer_conflict")
    return result


async def previous(session, key, request_hash):
    intent = await session.get(OrderIntent, key)
    if intent and intent.payload_hash != request_hash:
        fail("idempotency_conflict", "Key belongs to another checkout request")
    return MasterMonoState.model_validate(intent.response_json) if intent and intent.response_json else None


def binding_for(request, proof):
    return Binding(order_id=request.order_id, operator_id=request.operator_id, invoice_id=proof["invoice_id"],
        reference=request.reference, amount_minor=request.amount_minor, profile_id=request.profile_id,
        profile_hash=request.profile_hash, profile=request.profile, authority=request.authority,
        transport=request.transport, created_at=request.created_at, create_request_id=request.id,
        page_url="https://pay.mbnk.biz/" + proof["invoice_id"], create_proof=proof)


def matching_identity(proof, request):
    return bool(isinstance(proof, dict) and proof.get("verified") is True
        and proof.get("reference") == request.reference and proof.get("amount_minor") == request.amount_minor
        and proof.get("currency") == "UAH" and proof.get("profile_id") == request.profile_id
        and proof.get("merchant_id") == request.profile["merchant_id"] and proof.get("authority") == request.authority
        and proof.get("transport") == request.transport
        and proof.get("proof_hash") == digest({k:v for k,v in proof.items() if k != "proof_hash"}))


async def start(session, actor, order_id, payload, raw_key):
    from app.services.monobank_provider import invoice_payload, MonobankError
    try:
        actor, order = await actor_order(session, actor, order_id, lock=True)
        provider = guard(session, order.__dict__.get("master_mono_binding") or order.__dict__.get("master_mono_create_request"))
        if order.simulation:
            fail("legacy_simulation", "Historical simulated sales cannot be charged")
        key = operation_key(raw_key, f"master-mono-start:{actor.id}:{order_id}")
        request_hash = digest(payload.model_dump(mode="json"))
        await previous(session, key, request_hash)
        # Every key, browser retry and process restart returns this same durable owner.
        if order.__dict__.get("master_mono_create_request"):
            return view(session, order)
        guard(session, create=True)
        if order.revision != payload.expected_revision:
            fail("revision_conflict", "Reload the sale")
        if (order.__dict__.get("master_mono_binding") or order.payments or order.handovers or order.shipments
                or order.qr_payment_attempts or order.status not in {OrderStatus.pending, OrderStatus.confirmed}):
            fail("master_mono_checkout_owned", "Recover the existing sale checkout")
        from app.models.fiscal import FiscalDocument
        from app.services.fiscal_automation import owner
        if await owner(session, order.id) or await session.scalar(select(FiscalDocument.id).where(FiscalDocument.order_id == order.id)):
            fail("master_mono_issuer_conflict", "Another fiscal strategy already owns this sale")
        await local_sales.OrderService().validate_owned_reservations(session, order, order.items)
        basket, policy = basket_for(order)
        reference = str(uuid4())
        invoice_payload(reference, minor(order.total_amount), basket)
        intent = await claim_intent(session, key, request_hash)
        request = CreateRequest(order_id=order.id, operator_id=actor.id, reference=reference,
            amount_minor=minor(order.total_amount), profile_id=provider.profile["id"], profile_hash=digest(provider.profile),
            profile=provider.profile, authority="test" if provider.test_only else "provider",
            transport="mock_http" if provider.test_only else "https", basket=basket, basket_hash=digest(basket),
            policy=policy, created_at=datetime.now(UTC))
        session.add(request)
        order.revision += 1
        await session.flush()
        request_id, actor_id = request.id, actor.id
        intent.order_id = order_id
        await session.commit()  # Dispatch claim is durable before the only POST; no business locks cross I/O.
        try:
            proof = await provider.create_invoice(reference, request.amount_minor, basket)
            if (not matching_identity(proof, request) or proof.get("page_url") != "https://pay.mbnk.biz/" + proof.get("invoice_id", "")):
                raise MonobankError("monobank_proof_invalid")
        except Exception:
            # A timeout, malformed response or readiness failure never authorizes a second POST.
            _, order = await actor_order(session, SimpleNamespace(id=actor_id), order_id)
            return view(session, order)
        _, order = await actor_order(session, SimpleNamespace(id=actor_id), order_id, lock=True)
        if not order.__dict__.get("master_mono_binding"):
            request = await session.get(CreateRequest, request_id)
            guard(session, request)
            session.add(binding_for(request, proof))
            order.revision += 1
            await session.flush()
        result = view(session, await local_sales.load(session, order_id))
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def recover(session, actor, order_id, payload, raw_key):
    """Managers may attach an existing exact bank invoice; this path never POSTs."""
    try:
        actor, order = await actor_order(session, actor, order_id, manager=True)
        actor_id = actor.id
        request = order.__dict__.get("master_mono_create_request")
        if order.simulation or not request:
            fail("master_mono_request_required", "Recover a durable live invoice request")
        provider = guard(session, request)
        binding = order.__dict__.get("master_mono_binding")
        if binding:
            if binding.invoice_id != payload.invoice_id:
                fail("master_mono_identity_conflict", "The invoice is already bound")
            return view(session, order)
        if order.revision != payload.expected_revision:
            fail("revision_conflict", "Reload the sale")
        key = operation_key(raw_key, f"master-mono-recover:{actor.id}:{order_id}")
        request_hash = digest(payload.model_dump(mode="json"))
        await previous(session, key, request_hash)
        request_id = request.id
        await session.rollback()
        proof = await provider.inspect_invoice(payload.invoice_id)
        request = await session.get(CreateRequest, request_id)
        if not matching_identity(proof, request) or not dated_recovery(proof, request):
            fail("master_mono_recovery_mismatch", "The existing invoice must match reference, amount, merchant and creation time")
        _, order = await actor_order(session, SimpleNamespace(id=actor_id), order_id, lock=True, manager=True)
        guard(session, request)
        if order.__dict__.get("master_mono_binding") or order.revision != payload.expected_revision:
            fail("revision_conflict", "Reload the sale after concurrent recovery")
        intent = await claim_intent(session, key, request_hash)
        session.add(binding_for(request, proof))
        order.revision += 1
        await session.flush()
        result = view(session, await local_sales.load(session, order_id))
        intent.order_id, intent.response_json = order_id, result.model_dump(mode="json")
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


def dated_recovery(proof, request):
    try:
        created, observed = datetime.fromisoformat(proof["created_at"]), datetime.fromisoformat(proof["observed_at"])
        bound = request.created_at.replace(tzinfo=UTC)
        return (created.tzinfo is not None and observed.tzinfo is not None
            and bound - timedelta(seconds=30) <= created <= bound + timedelta(minutes=15)
            and created <= observed <= datetime.now(UTC) + timedelta(seconds=30))
    except (KeyError, TypeError, ValueError):
        return False


async def refresh(session, actor, order_id, payload, raw_key):
    from app.services.monobank_provider import MonobankError
    try:
        actor, order = await actor_order(session, actor, order_id)
        actor_id = actor.id
        binding = order.__dict__.get("master_mono_binding")
        provider = guard(session, binding)
        if not binding:
            fail("master_mono_binding_required", "Create the invoice first")
        key = operation_key(raw_key, f"master-mono-refresh:{actor.id}:{order_id}")
        request_hash = digest(payload.model_dump(mode="json"))
        replay = await previous(session, key, request_hash)
        if replay:
            return replay
        if order.revision != payload.expected_revision:
            fail("revision_conflict", "Reload the same sale")
        invoice_id, reference, amount = binding.invoice_id, binding.reference, binding.amount_minor
        await session.rollback()
        payment_proof = fiscal_proof = None
        try:
            payment_proof = await provider.get_invoice(invoice_id, reference=reference, amount_minor=amount)
            status = payment_proof["status"]
            if status == "success":
                fiscal_proof = await provider.get_fiscal_checks(invoice_id)
        except MonobankError as error:
            status = error.code
        actor, order = await actor_order(session, SimpleNamespace(id=actor_id), order_id, lock=True)
        binding = order.master_mono_binding
        guard(session, binding)
        replay = await previous(session, key, request_hash)
        if replay:
            return replay
        if order.revision != payload.expected_revision or binding.invoice_id != invoice_id:
            fail("revision_conflict", "Reload the same sale after concurrent refresh")
        intent = await claim_intent(session, key, request_hash)
        now = datetime.now(UTC)
        if direct.invoice_valid(payment_proof, binding, invoice_id) and dated_invoice(payment_proof, binding):
            evidence = order.__dict__.get("master_mono_payment")
            if not evidence:
                if order.payments:
                    fail("master_mono_payment_conflict", "Existing payment belongs to another checkout")
                payment = OrderPayment(order_id=order.id, operator_id=binding.operator_id, amount_minor=amount,
                    source="master_mono_provider", method="card", evidence=invoice_id, simulation=False, created_at=now)
                session.add(payment)
                await session.flush()
                evidence = Payment(order_id=order.id, binding_id=binding.id, payment_id=payment.id,
                    proof=payment_proof, proof_hash=digest(payment_proof), created_at=now)
                session.add(evidence)
                order.payment_method = "card"
                await session.flush()
                order = await local_sales.load(session, order_id)
            if not canonical_payment(order):
                fail("master_mono_payment_conflict", "The original payment proof is invalid")
            if fiscal_proof:
                status, check = direct.check_proof(fiscal_proof, binding, invoice_id)
                receipt = order.__dict__.get("master_mono_receipt")
                if status == "accepted" and receipt and receipt.receipt_id != check["id"]:
                    status = "mono_checkbox_receipt_conflict"
                elif status == "accepted" and not receipt:
                    receipt = Receipt(order_id=order.id, binding_id=binding.id, payment_id=evidence.payment_id,
                        receipt_id=check["id"], proof=fiscal_proof, proof_hash=digest(fiscal_proof), created_at=now)
                    session.add(receipt)
                    await session.flush()
                if (status == "accepted" and receipt and check.get("file") and not pdf_bytes(receipt)
                        and not receipt.__dict__.get("artifact")):
                    session.add(Artifact(order_id=order.id, receipt_id=receipt.id, proof=fiscal_proof,
                        proof_hash=digest(fiscal_proof), created_at=now))
            elif status == "success":
                status = "mono_checkbox_receipt_pending"
        elif payment_proof and status == "success":
            status = "master_mono_proof_invalid"
        observed_fiscal = deepcopy(fiscal_proof)
        if observed_fiscal:
            for check in observed_fiscal["checks"]:
                if check.get("file"):
                    check["file_sha256"] = sha256(check.pop("file").encode()).hexdigest()
            observed_fiscal["proof_hash"] = digest({k:v for k,v in observed_fiscal.items() if k != "proof_hash"})
        proof = {"payment":payment_proof, "fiscal":observed_fiscal}
        session.add(Observation(order_id=order.id, status=status, proof=proof, proof_hash=digest(proof), created_at=now))
        order.revision += 1
        await session.flush()
        order = await local_sales.load(session, order_id)
        result = view(session, order)
        intent.order_id, intent.response_json = order_id, result.model_dump(mode="json")
        guard(session, binding)
        await session.commit()
        return result
    except IntegrityError:
        await session.rollback()
        fail("master_mono_identity_conflict", "Invoice or receipt already belongs to another checkout")
    except Exception:
        await session.rollback()
        raise


async def validate_handover(session, actor, order):
    actor, order = await actor_order(session, actor, order.id)
    guard(session, order.master_mono_binding)
    observations = order.__dict__.get("master_mono_observations", [])
    if (order.created_by_user_id != actor.id or not accepted_receipt(order) or not observations
            or (datetime.now(UTC) - observations[-1].created_at.replace(tzinfo=UTC)).total_seconds() > 30):
        fail("master_mono_receipt_required", "The exact payment and latest accepted external receipt are required")


async def download_pdf(session, actor, order_id):
    _, order = await actor_order(session, actor, order_id)
    guard(session, order.__dict__.get("master_mono_binding"))
    receipt = accepted_receipt(order)
    content = pdf_bytes(receipt)
    if not content:
        fail("master_mono_pdf_unavailable", "No accepted external receipt PDF is available", 404)
    return receipt.receipt_id, content

