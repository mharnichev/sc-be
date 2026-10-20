"""Customer checkout dispatch; existing Shop ledgers own money and external receipts.

A durable request fences the only invoice POST. Unknown outcomes require an
operator to recover an existing invoice using GET-only provider inspection.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from jose import JWTError
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_token
from app.models.admin_user import AdminUser
from app.models.order import OrderIntent, OrderStatus
from app.models.shop_payments import ShopPaymentIntent
from app.models.shop_online_payment import ShopOnlinePaymentRequest as Request
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_online_payment import OnlinePaymentState
from app.services import shop_payments as payments, shop_sales as sales, shop_mono_checkbox as direct
from app.services.order_documents import claim_intent, digest, minor, operation_key
from app.services.monobank_provider import MonobankGateway, MonobankError, invoice_payload, payment_page_url


def gateway():
    return MonobankGateway(create_capability="shop")


def fiscal_policy():
    policy = settings.shop_online_payment_fiscal_policy
    # This merchant's Checkbox tax mapping is an explicit deployment decision.
    # Code 8 must match the linked Checkbox register; never infer it from VAT.
    if (not isinstance(policy, dict) or set(policy) != {"id", "tax_codes", "unit"}
            or not isinstance(policy.get("id"), str) or not policy["id"].strip() or len(policy["id"]) > 64
            or policy.get("tax_codes") != [8] or any(type(c) is not int for c in policy.get("tax_codes", []))
            or policy.get("unit") != "шт."):
        sales.fail("shop_online_fiscal_policy_required", "Approve the explicit Checkbox tax code 8 and unit policy")
    return deepcopy(policy)


def config_ready(provider=None):
    provider = provider or gateway()
    try:
        fiscal_policy()
    except HTTPException:
        return False
    return bool(settings.shop_online_payment_enabled and settings.shop_online_payment_invoice_create_enabled
        and settings.shop_online_payment_link_confirmed and settings.shop_online_payment_operator_id
        and settings.mono_checkbox_auto_enabled and not direct.configuration_reasons()
        and provider.available and provider.external_checkbox and provider.profile
        and provider.profile["environment"] == "production"
        and ((provider.test_only and settings.app_env in {"local", "test"})
            or (not provider.test_only and settings.app_env in {"staging", "production"})))


async def operator(session, order):
    identifier = settings.shop_online_payment_operator_id
    if not identifier:
        sales.fail("shop_online_operator_unavailable", "Configure an active Shop payment operator", 503)
    return await sales.current_actor(session, SimpleNamespace(id=identifier), "read", order)


async def capabilities(session):
    if not config_ready():
        return {"enabled": False}
    try:
        await operator(session, SimpleNamespace(fulfillment_method=None))
        await payments.guard(session, gateway())
    except HTTPException:
        return {"enabled": False}
    return {"enabled": True}


async def authorize(session, order_id, customer, token, *, lock=False):
    order = await sales.load_order(session, order_id, lock=lock)
    if customer is not None and order.customer_id == customer.id:
        return order
    if customer is not None or order.customer_id is not None or not token:
        raise HTTPException(404, "Payment not found")
    try:
        claims = decode_token(token)
        if claims.get("scope") != "guest_order_payment" or claims.get("sub") != str(order.id):
            raise ValueError
        if (not order.guest_tracking_expires_at or order.guest_tracking_revoked_at
                or sales.aware(order.guest_tracking_expires_at) <= datetime.now(UTC)):
            raise ValueError
    except (JWTError, ValueError, TypeError):
        raise HTTPException(404, "Payment not found") from None
    return order


def basket_for(order):
    policy = fiscal_policy()
    lines = sorted(order.items, key=lambda i: i.id)
    if not lines or any(type(i.quantity) is not int or i.quantity <= 0 for i in lines):
        sales.fail("shop_online_basket_invalid", "Positive server priced goods are required")
    weights = [minor(i.total_price if i.total_price is not None else i.price * i.quantity) for i in lines]
    amount, gross = minor(order.total_amount), sum(weights)
    if amount <= 0 or gross < amount or any(w <= 0 for w in weights):
        sales.fail("shop_online_basket_invalid", "The server basket must have a positive supported total")
    allocated = [amount * weight // gross for weight in weights]
    for index in sorted(range(len(lines)), key=lambda n: (-(amount * weights[n] % gross), lines[n].id))[:amount-sum(allocated)]:
        allocated[index] += 1
    basket = []
    for item, total in zip(lines, allocated):
        each, remainder = divmod(total, item.quantity)
        if each <= 0:
            sales.fail("shop_online_basket_invalid", "Discounted goods need positive unit prices")
        for quantity, price in ((item.quantity-remainder, each), (remainder, each+1)):
            if quantity:
                basket.append(dict(name=item.product_name, qty=quantity, sum=price, total=quantity*price,
                    code=str(item.product_id), unit=policy["unit"], tax=policy["tax_codes"]))
    try:
        invoice_payload(str(uuid4()), amount, basket)
    except MonobankError:
        sales.fail("shop_online_basket_invalid", "Goods exceed the supported fiscal basket contract")
    return basket, policy


async def guard(session, order, *, create=False):
    if not settings.shop_online_payment_enabled or not settings.shop_online_payment_link_confirmed:
        sales.fail("shop_online_payment_disabled", "Shop online payment is closed", 503)
    fiscal_policy()
    if create and not config_ready(gateway()):
        sales.fail("shop_online_payment_disabled", "Shop invoice creation is closed", 503)
    actor = await operator(session, order)
    provider = gateway()
    await payments.guard(session, provider)
    await direct.guard(session, order)
    request = order.__dict__.get("shop_online_payment_request")
    if request:
        if request.operator_id != actor.id or request.policy != fiscal_policy():
            sales.fail("shop_online_request_conflict", "The original checkout configuration is required")
        payments.pinned(request, provider)
    return actor, provider


async def view(session, order):
    request = order.__dict__.get("shop_online_payment_request")
    binding = order.__dict__.get("shop_payment_intent")
    paid = bool(sales.canonical_payment(order))
    receipt = direct.accepted_receipt(order)
    enabled = bool(config_ready() and sales.eligible_order(order) and not sales.is_cod_order(order)
        and order.payment_method in {"onlineCard", "card"} and order.status not in {OrderStatus.cancelled, OrderStatus.completed})
    try:
        await guard(session, order)
    except HTTPException:
        enabled = False
    status = "not_started"
    if request:
        status = "creation_unknown" if not binding or not binding.invoice_id else "waiting"
    elif binding or order.payments:
        status = "requires_review"
    if paid:
        status = "accepted" if receipt else "paid_receipt_pending"
    observations = order.__dict__.get("shop_mono_checkbox_observations", [])
    if observations and observations[-1].status not in {"accepted", "mono_checkbox_receipt_pending", "monobank_unavailable"}:
        status = "requires_review"
    if request and binding and binding.invoice_id and not paid:
        audit = await session.scalar(select(ShopOperationAudit).where(
            ShopOperationAudit.order_id == order.id, ShopOperationAudit.operation == "r2_verify")
            .order_by(ShopOperationAudit.id.desc()).limit(1))
        if audit and audit.facts.get("intent_id") == binding.id and audit.facts.get("status") in {"failure", "expired", "reversed", "monobank_proof_invalid", "monobank_profile_mismatch"}:
            status = "requires_review"
    if order.status == OrderStatus.cancelled or (request and not enabled and not receipt):
        status = "requires_review"
    # Only expose a validated, pinned invoice payment page for this owned request.
    page = payment_page_url(binding.invoice_id) if request and binding and binding.invoice_id and status == "waiting" and enabled else None
    check = receipt.proof["checks"][0] if receipt else {}
    return OnlinePaymentState(order_id=order.id, enabled=enabled, status=status, paid=paid,
        receipt_accepted=bool(receipt), page_url=page, receipt_url=check.get("taxUrl"), amount_minor=minor(order.total_amount))


async def read(session, order_id, customer=None, token=None):
    return await view(session, await authorize(session, order_id, customer, token))


async def start(session, order_id, customer, token, raw_key):
    try:
        order = await authorize(session, order_id, customer, token, lock=True)
        actor, provider = await guard(session, order)
        key = operation_key(raw_key, f"shop-online:{order.id}")
        request_hash = digest({"order_id": order.id})
        previous = await session.get(OrderIntent, key)
        if previous and previous.payload_hash != request_hash:
            sales.fail("idempotency_conflict", "Key belongs to another request")
        # The durable order owner wins even when a retry uses a fresh key.
        if order.__dict__.get("shop_online_payment_request"):
            return await view(session, order)
        await guard(session, order, create=True)
        payments.validate_order(order, order.revision)
        if sales.is_cod_order(order) or order.payment_method != "onlineCard":
            sales.fail("shop_online_prepayment_required", "Choose online card payment at checkout")
        if order.__dict__.get("shop_payment_intent"):
            sales.fail("payment_intent_exists", "Recover the existing payment workflow")
        await payments.admit(session, order, "card")
        from app.services.order import OrderService
        await OrderService.validate_owned_reservations(session, order, order.items)
        basket, policy = basket_for(order)
        intent = await claim_intent(session, key, request_hash)
        now, reference = datetime.now(UTC), str(uuid4())
        binding = ShopPaymentIntent(id=str(uuid4()), order_id=order.id, mode="prepayment", reference=reference,
            amount_minor=minor(order.total_amount), profile_id=provider.profile["id"], profile=deepcopy(provider.profile),
            profile_hash=digest(provider.profile), authority="test" if provider.test_only else "provider",
            transport="mock_http" if provider.test_only else "https", operator_id=actor.id, created_at=now)
        session.add(binding)
        await session.flush()
        request = Request(order_id=order.id, intent_id=binding.id, operator_id=actor.id,
            customer_id=customer.id if customer else None, initiator="customer" if customer else "guest",
            reference=reference, amount_minor=binding.amount_minor, profile_id=binding.profile_id,
            profile_hash=binding.profile_hash, profile=deepcopy(binding.profile), authority=binding.authority,
            transport=binding.transport, basket=basket, basket_hash=digest(basket), policy=policy, created_at=now)
        session.add(request)
        order.revision += 1
        intent.order_id = order.id
        await session.flush()
        request_id, amount, dispatch_revision = request.id, request.amount_minor, order.revision
        await session.commit()  # No business lock spans I/O; this irrevocably fences dispatch.
        try:
            # Recheck all Shop flags and current operator immediately before dispatch.
            order = await sales.load_order(session, order_id)
            await guard(session, order, create=True)
            await session.rollback()
            async def before_dispatch():
                current = await authorize(session, order_id, customer, token)
                await guard(session, current, create=True)
                payments.validate_order(current, dispatch_revision)
                if (not current.shop_online_payment_request or current.shop_online_payment_request.id != request_id
                        or current.shop_payment_intent.invoice_id is not None):
                    sales.fail("shop_online_request_conflict", "Recover the existing immutable checkout")
                await session.rollback()
            proof = await provider.create_invoice(reference, amount, basket, before_dispatch=before_dispatch)
            request = await session.get(Request, request_id)
            if not matching_identity(proof, request):
                raise ValueError("Invalid creation proof")
        except Exception:
            await session.rollback()
            return await read(session, order_id, customer, token)
        order = await authorize(session, order_id, customer, token, lock=True)
        actor, _ = await guard(session, order)
        request = await session.get(Request, request_id)
        if not matching_identity(proof, request):
            sales.fail("monobank_proof_invalid", "Invoice proof does not match checkout")
        binding = order.shop_payment_intent
        if binding.invoice_id and binding.invoice_id != proof["invoice_id"]:
            sales.fail("invoice_binding_conflict", "Recover the original invoice")
        binding.invoice_id = proof["invoice_id"]
        order.revision += 1
        session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id, operation="online_invoice_created",
            request_key=key, facts={"request_id":request.id,"proof":proof}, created_at=datetime.now(UTC)))
        await session.commit()
        return await read(session, order_id, customer, token)
    except Exception:
        await session.rollback()
        raise


def matching_identity(proof, request):
    return bool(isinstance(proof, dict) and proof.get("verified") is True
        and type(proof.get("amount_minor")) is int and proof["amount_minor"] == request.amount_minor
        and proof.get("reference") == request.reference and proof.get("currency") == "UAH"
        and proof.get("profile_id") == request.profile_id and proof.get("merchant_id") == request.profile["merchant_id"]
        and proof.get("authority") == request.authority and proof.get("transport") == request.transport
        and proof.get("proof_hash") == digest({k:v for k,v in proof.items() if k != "proof_hash"}))


async def recover(session, actor, order_id, payload, raw_key):
    try:
        order = await sales.load_order(session, order_id)
        actor = await sales.current_actor(session, actor, "read", order)
        if not actor.is_superuser and not actor.can_manage_fiscal_settings:
            raise HTTPException(403, "Manager recovery permission required")
        actor_id = actor.id
        _, provider = await guard(session, order)
        request = order.__dict__.get("shop_online_payment_request")
        if not request:
            sales.fail("shop_online_recovery_conflict", "Reload the durable checkout request")
        if order.shop_payment_intent.invoice_id:
            if order.shop_payment_intent.invoice_id != payload.invoice_id:
                sales.fail("invoice_binding_conflict", "Recover the original invoice")
            return await view(session, order)
        if order.revision != payload.expected_revision:
            sales.fail("revision_conflict", "Reload the durable checkout request")
        key = operation_key(raw_key, f"shop-online-recovery:{actor_id}:{order_id}")
        request_hash = digest(payload.model_dump(mode="json"))
        request_id = request.id
        await session.rollback()
        proof = await provider.inspect_invoice(payload.invoice_id)  # GET only, never invoice/create.
        request = await session.get(Request, request_id)
        created = datetime.fromisoformat(proof.get("created_at", ""))
        if (not matching_identity(proof, request) or created.tzinfo is None
                or created < sales.aware(request.created_at)-timedelta(seconds=30)
                or created > sales.aware(request.created_at)+timedelta(minutes=15)
                or created > datetime.now(UTC)+timedelta(seconds=30)):
            sales.fail("shop_online_recovery_mismatch", "Invoice must match the exact durable checkout")
        order = await sales.load_order(session, order_id, lock=True)
        actor = await sales.current_actor(session, SimpleNamespace(id=actor_id), "read", order)
        if not actor.is_superuser and not actor.can_manage_fiscal_settings:
            raise HTTPException(403, "Manager recovery permission required")
        await guard(session, order)
        if order.revision != payload.expected_revision or order.shop_payment_intent.invoice_id:
            sales.fail("revision_conflict", "Reload after concurrent recovery")
        await claim_intent(session, key, request_hash)
        order.shop_payment_intent.invoice_id = payload.invoice_id
        order.revision += 1
        session.add(ShopOperationAudit(order_id=order.id,operator_id=actor.id,operation="online_invoice_recovered",
            request_key=key,facts={"request_id":request.id,"proof":proof},created_at=datetime.now(UTC)))
        await session.commit()
        return await view(session, await sales.load_order(session, order_id))
    except Exception:
        await session.rollback()
        raise
