"""Customer checkout dispatch; existing Shop ledgers own money and external receipts.

A durable request fences the only invoice POST. Unknown outcomes require an
operator to recover an existing invoice using GET-only provider inspection.
"""
import asyncio

from app.core import error_reporting
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from jose import JWTError
from sqlalchemy import func, select

from app.core.config import settings
from app.core.security import decode_token
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderIntent, OrderStatus
from app.models.shop_payments import ShopPaymentIntent
from app.models.shop_online_payment import ShopOnlinePaymentRequest as Request
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_online_payment import OnlinePaymentActions, OnlinePaymentOrder, OnlinePaymentState
from app.services import shop_payments as payments, shop_sales as sales, shop_mono_checkbox as direct
from app.services import order_archive_payments
from app.services.order_documents import claim_intent, digest, minor, operation_key
from app.services.monobank_provider import MonobankGateway, MonobankError, invoice_payload, payment_page_url, shop_iframe_page_url, shop_invoice_icon


SHOP_INVOICE_VALIDITY = 3 * 86400
LEGACY_INVOICE_VALIDITY = 900


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
    return {"enabled": True, "presentation_modes": ["redirect", "iframe"],
        "iframe_min_width": 576, "iframe_min_height": 576}


async def authorize(session, order_id, customer, token, *, lock=False):
    order = await sales.load_order(session, order_id, lock=lock)
    return await authorized_order(session, order, customer, token)


async def authorized_order(session, order, customer, token):
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
        if "jti" in claims:
            from app.services.checkout_reminders import payment_email_token_allowed
            if not await payment_email_token_allowed(session, order.id, token):
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
        icon = shop_invoice_icon(getattr(item, "product_image_url", None))
        for quantity, price in ((item.quantity-remainder, each), (remainder, each+1)):
            if quantity:
                basket.append(dict(name=item.product_name, qty=quantity, sum=price, total=quantity*price,
                    code=str(item.product_id), unit=policy["unit"], tax=policy["tax_codes"],
                    **({"icon": icon} if icon else {})))
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


def pre_dispatch_payment_methods(order):
    """Only an untouched checkout can change method without a bank closure proof."""
    if (not settings.shop_order_operations_enabled or not settings.shop_online_payment_enabled
            or not sales.eligible_order(order) or order.status not in {OrderStatus.pending, OrderStatus.confirmed}
            or order.payment_method != "onlineCard" or minor(order.total_amount) <= 0):
        return []
    facts = ("shop_online_payment_request", "shop_payment_intent", "payments", "qr_payment_attempts",
        "handovers", "shipments", "shop_payment_confirmation", "shop_payment_evidence",
        "shop_fulfillment_fact", "shop_completion", "shop_payment_resolutions", "shop_carrier_returns",
        "shop_np_returns", "shop_fiscal_documents", "shop_financial_returns", "shop_checkbox_binding",
        "shop_checkbox_documents", "shop_checkbox_returns", "shop_mono_checkbox_receipt",
        "shop_mono_checkbox_observations", "master_mono_create_request", "master_mono_binding",
        "master_mono_payment", "master_mono_receipt", "master_mono_observations")
    # Missing/expired relations are unknown, not proof that dispatch never happened.
    if any(name not in order.__dict__ or order.__dict__[name] for name in facts):
        return []
    return ["cash"] if order.fulfillment_method == "pickup" else ["cashOnDelivery"]


def presentation_facts(request, mode, validity=None):
    facts = {"schema_version": 1, "order_id": request.order_id, "request_id": request.id,
        "intent_id": request.intent_id, "reference": request.reference, "presentation": mode}
    if validity is not None:
        facts.update(schema_version=2, validity_seconds=validity)
    return facts


async def selected_presentation(session, request):
    audits = (await session.scalars(select(ShopOperationAudit).where(
        ShopOperationAudit.order_id == request.order_id,
        ShopOperationAudit.operation == "online_presentation_selected").limit(2))).all()
    if not audits:
        return "redirect", LEGACY_INVOICE_VALIDITY  # Before presentation support.
    if len(audits) != 1:
        return None
    audit = audits[0]
    mode = audit.facts.get("presentation") if isinstance(audit.facts, dict) else None
    if not isinstance(mode, str) or mode not in {"redirect", "iframe"}:
        return None
    validity = audit.facts.get("validity_seconds")
    if validity is not None and (type(validity) is not int or not 60 <= validity <= 30 * 86400):
        return None
    facts = presentation_facts(request, mode, validity)
    if validity is not None and audit.facts.get("schema_version") == 3:
        facts["schema_version"] = 3  # Read records from the previous release candidate.
    if (audit.facts != facts or audit.request_key != digest(["shop-online-presentation", request.id])
            or audit.operator_id != request.operator_id):
        return None
    return mode, validity if validity is not None else LEGACY_INVOICE_VALIDITY


def creation_embed_url(proof, request, mode, invoice_id):
    if (not matching_identity(proof, request) or proof.get("invoice_id") != invoice_id
            or proof.get("display_type", "redirect") != mode
            or not isinstance(proof.get("page_url"), str)):
        raise MonobankError("monobank_proof_invalid")
    if mode == "redirect":
        payment_page_url(invoice_id, proof.get("page_url"))
        return None
    verification = proof.get("invoice_verification")
    if not matching_identity(verification, request) or verification.get("invoice_id") != invoice_id:
        raise MonobankError("monobank_proof_invalid")
    if proof.get("iframe_fallback") is True:
        payment_page_url(invoice_id, proof["page_url"])
        return None
    return shop_iframe_page_url(invoice_id, proof.get("page_url"))


async def payment_presentation(session, request, binding):
    selection = await selected_presentation(session, request)
    if selection is None:
        return None, True, None
    mode, validity = selection
    expires_at = sales.aware(request.created_at) + timedelta(seconds=validity)
    if not binding or not binding.invoice_id:
        return None, False, expires_at
    audits = (await session.scalars(select(ShopOperationAudit).where(
        ShopOperationAudit.order_id == request.order_id,
        ShopOperationAudit.operation == "online_invoice_created").limit(2))).all()
    if not audits:
        return None, False, expires_at  # GET-only recovery has no returned widget URL.
    if len(audits) != 1:
        return None, True, expires_at
    audit = audits[0]
    facts = audit.facts if isinstance(audit.facts, dict) else {}
    if facts.get("request_id") != request.id or audit.operator_id != request.operator_id:
        return None, True, expires_at
    try:
        return creation_embed_url(facts.get("proof"), request, mode, binding.invoice_id), False, expires_at
    except MonobankError:
        return None, True, expires_at


async def attempt_fact(session, order_id, request_id, operation):
    return await session.scalar(select(ShopOperationAudit).where(
        ShopOperationAudit.order_id == order_id,
        ShopOperationAudit.operation == operation,
        ShopOperationAudit.request_key == digest([operation, request_id]),
    ))


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
    bank_status = None
    if request and binding and binding.invoice_id and not paid:
        blocked = {"success", "failure", "expired", "reversed", "monobank_reversed",
            "monobank_proof_invalid", "monobank_profile_mismatch"}
        bank_status = await session.scalar(select(ShopOperationAudit.facts["status"].as_string()).where(
            ShopOperationAudit.order_id == order.id, ShopOperationAudit.operation == "r2_verify",
            ShopOperationAudit.facts["intent_id"].as_string() == binding.id,
            ShopOperationAudit.facts["status"].as_string().in_(blocked))
            .order_by(ShopOperationAudit.id.desc()).limit(1))
        if bank_status is None:
            recovered = await session.scalar(select(ShopOperationAudit).where(
                ShopOperationAudit.order_id == order.id, ShopOperationAudit.operation == "online_invoice_recovered")
                .order_by(ShopOperationAudit.id.desc()).limit(1))
            if recovered:
                proof = recovered.facts.get("proof")
                if (recovered.facts.get("request_id") != request.id or not matching_identity(proof, request)
                        or proof.get("invoice_id") != binding.invoice_id):
                    bank_status = "monobank_proof_invalid"
                elif proof.get("has_cancellations") or proof.get("status") in blocked:
                    bank_status = "reversed" if proof.get("has_cancellations") else proof["status"]
        if bank_status:
            status = "requires_review"
    if status == "creation_unknown" and await attempt_fact(session, order.id, request.id, "online_pre_dispatch_failed"):
        status = "creation_failed"
    if (order.status == OrderStatus.cancelled or (request and not enabled and status not in {"creation_failed", "creation_unknown"} and not receipt)
            or (not paid and status not in {"creation_failed", "creation_unknown"}
                and (order.shop_payment_resolutions or order.shop_financial_returns
                or order.shop_carrier_returns or order.shop_np_returns or order.payments))):
        status = "requires_review"
    embed, presentation_conflict, expires_at = (
        await payment_presentation(session, request, binding) if request else (None, False, None))
    if presentation_conflict:
        status = "requires_review"
    if status == "waiting" and expires_at and expires_at <= datetime.now(UTC):
        status, bank_status = "requires_review", "expired"
    # Only expose a validated, pinned invoice payment page for this owned request.
    page = payment_page_url(binding.invoice_id) if request and binding and binding.invoice_id and status == "waiting" and enabled else None
    check = receipt.proof["checks"][0] if receipt else {}
    reasons = {"not_started": "payment_not_started", "creation_unknown": "invoice_creation_unknown",
        "creation_failed": "invoice_pre_dispatch_failed",
        "waiting": "payment_pending", "paid_receipt_pending": "payment_verified_receipt_pending",
        "accepted": "payment_accepted", "requires_review": "payment_review_required"}
    reason = reasons[status]
    if status == "requires_review" and not paid:
        reason = {"failure": "payment_failed", "expired": "invoice_expired", "reversed": "payment_reversed",
            "monobank_reversed": "payment_reversed"}.get(bank_status, reason)
    if order.status in {OrderStatus.cancelled, OrderStatus.completed}:
        reason = "order_closed"
    elif not request and not binding and not paid and order.payment_method != "onlineCard":
        reason = "offline_payment_selected"
    elif not enabled and status == "not_started":
        reason = "payment_unavailable"
    methods = pre_dispatch_payment_methods(order)
    cancellable = (order.payment_method == "onlineCard" and not paid
        and order.status in {OrderStatus.pending, OrderStatus.confirmed}
        and settings.shop_order_operations_enabled and settings.shop_online_payment_operator_id
        and await session.scalar(select(func.shop_online_cancel_eligible(order.id))))
    actions = OnlinePaymentActions(cancel=bool(cancellable), start=bool(enabled and status == "not_started" and methods),
        continue_payment=bool(page), refresh=status != "accepted",
        change_method=bool(methods), contact_support=status in {"creation_failed", "creation_unknown", "requires_review"}
            or reason == "payment_unavailable")
    # No bank status in this integration establishes irreversible unpaid closure.
    # In particular, failure/expired never authorize another invoice or COD.
    failure = order.payment_failure
    return OnlinePaymentState(order_id=order.id, enabled=enabled, status=status, paid=paid,
        payment_status="failed" if failure else None, payment_failure=failure,
        receipt_accepted=bool(receipt), page_url=page, redirect_url=page,
        embed_url=embed if page else None, presentation_mode="iframe" if embed and page else "redirect",
        receipt_url=check.get("taxUrl"), amount_minor=minor(order.total_amount),
        reason=reason, actions=actions, available_payment_methods=methods, revision=order.revision,
        attempt_id=request.id if request else None, order=OnlinePaymentOrder(id=order.id, number=str(order.id),
            status=order.status.value, fulfillment_method=order.fulfillment_method,
            item_count=sum(item.quantity for item in order.items), payment_method=order.payment_method))


async def verify_reminder_invoice(session, order):
    """Require a positive unpaid check before emailing about a bound invoice."""
    binding = order.shop_payment_intent
    request = order.shop_online_payment_request
    try:
        _, provider = await guard(session, order)
        proof = await provider.get_invoice(binding.invoice_id, reference=binding.reference,
            amount_minor=binding.amount_minor)
    except HTTPException:
        return None
    except MonobankError as error:
        return False if error.code in {"monobank_reversed", "monobank_proof_invalid", "monobank_profile_mismatch"} else None
    if not matching_identity(proof, request) or proof["invoice_id"] != binding.invoice_id:
        return False
    if proof["status"] in {"processing", "hold"}:
        return None
    return proof["status"] == "created"


async def customer_cancel(session, order_id, customer, token, payload, raw_key):
    """Reuse the cancellation command for an invoice that has not been sent."""
    from app.schemas.shop_online_payment import OnlinePaymentCancellationCommand
    from app.services import shop_online_cancellation

    try:
        order = await authorize(session, order_id, customer, token, lock=True)
        attempt = order.shop_online_payment_request
        if payload.expected_attempt_id != (attempt.id if attempt else None):
            sales.fail("shop_online_attempt_conflict", "Reload the current payment attempt")
        actor = await operator(session, order)
        command = OnlinePaymentCancellationCommand(expected_revision=payload.expected_revision)
        key = operation_key(raw_key, f"customer-shop-cancel:{order.id}")
        await shop_online_cancellation.cancel(session, actor, order.id, command, key)
        return await read(session, order_id, customer, token)
    except Exception:
        await session.rollback()
        raise


async def read(session, order_id, customer=None, token=None):
    return await view(session, await authorize(session, order_id, customer, token))


async def change_method(session, order_id, customer, token, payload, raw_key):
    """Change the existing, never-dispatched order in one idempotent transaction."""
    try:
        order = await authorize(session, order_id, customer, token, lock=True)
        key = operation_key(raw_key, f"shop-online-method:{order.id}")
        request_hash = digest(payload.model_dump(mode="json"))
        command = await claim_intent(session, key, request_hash)
        if command.response_json:
            return await view(session, order)
        if order.revision != payload.expected_revision:
            sales.fail("revision_conflict", "Reload the current payment state")
        attempt = order.__dict__.get("shop_online_payment_request")
        if payload.expected_attempt_id != (attempt.id if attempt else None):
            sales.fail("shop_online_attempt_conflict", "Reload the current payment attempt")
        if payload.payment_method not in pre_dispatch_payment_methods(order):
            sales.fail("shop_online_method_change_blocked", "An existing bank attempt requires financial reconciliation")
        payments.validate_order(order, payload.expected_revision)
        from app.services.order import OrderService
        await OrderService.validate_owned_reservations(session, order, order.items)
        order.payment_method = payload.payment_method
        order.revision += 1
        command.order_id = order.id
        await session.flush()
        result = await view(session, order)
        command.response_json = result.model_dump(mode="json")
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def record_start_failure(session, order_id, request_id, dispatch_possible, verified_reply, error):
    order = await sales.load_order(session, order_id, lock=True)
    request = order.shop_online_payment_request
    binding = order.shop_payment_intent
    if order.status == OrderStatus.cancelled or not request or request.id != request_id or not binding or binding.invoice_id:
        await session.rollback()
        return
    stage = error.stage if isinstance(error, MonobankError) else None
    if not dispatch_possible:
        code = "merchant_readiness_failed" if stage == "merchant_readiness" else "invoice_pre_dispatch_failed"
    elif verified_reply:
        code = "invoice_binding_unconfirmed"
    elif stage == "create_response_url":
        code = "payment_page_unverified"
    elif stage == "create_response_body":
        code = "invoice_response_invalid"
    else:
        code = "invoice_result_unconfirmed"
    session.add(ShopOperationAudit(order_id=order.id, operator_id=request.operator_id,
        operation="online_payment_failed", request_key=digest(["shop-online-failed", request.id]),
        facts={"request_id": request.id, "code": code}, created_at=datetime.now(UTC)))
    await session.commit()


async def start(session, order_id, customer, token, raw_key, *, presentation="redirect"):
    if presentation not in {"redirect", "iframe"}:
        raise HTTPException(422, "Invalid payment presentation")
    # The auth dependency shares this session. Rollback expires ORM instances,
    # while dispatch and fallback authorization need only the authenticated ID.
    customer = SimpleNamespace(id=customer.id) if customer is not None else None
    try:
        order = await authorize(session, order_id, customer, token, lock=True)
        actor, provider = await guard(session, order)
        key = operation_key(raw_key, f"shop-online:{order.id}")
        request_payload = {"order_id": order.id}
        if presentation == "iframe":
            request_payload["presentation"] = presentation
        request_hash = digest(request_payload)
        previous = await session.get(OrderIntent, key)
        if previous and previous.payload_hash != request_hash:
            sales.fail("idempotency_conflict", "Key belongs to another request")
        # The durable order owner wins even when a retry uses a fresh key.
        if order.__dict__.get("shop_online_payment_request"):
            replay = await claim_intent(session, key, request_hash)
            replay.order_id = order.id
            state = await view(session, order)
            await session.commit()
            return state
        await guard(session, order, create=True)
        payments.validate_order(order, order.revision)
        if sales.is_cod_order(order) or order.payment_method != "onlineCard":
            sales.fail("shop_online_prepayment_required", "Choose online card payment at checkout")
        if order.__dict__.get("shop_payment_intent"):
            sales.fail("payment_intent_exists", "Recover the existing payment workflow")
        if not pre_dispatch_payment_methods(order):
            sales.fail("shop_online_method_change_blocked", "Existing financial or fulfillment facts prevent new payment")
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
        session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id,
            operation="online_presentation_selected", request_key=digest(["shop-online-presentation", request.id]),
            facts=presentation_facts(request, presentation, SHOP_INVOICE_VALIDITY), created_at=now))
        dispatch_facts = {"request_id": request.id, "intent_id": binding.id}
        session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id,
            operation="online_dispatch_prepared", request_key=digest(["shop-online-prepared", request.id]),
            facts=dispatch_facts, created_at=now))
        request_id, amount, dispatch_revision = request.id, request.amount_minor, order.revision
        attempt_operator_id = actor.id
        await session.commit()  # Persist the attempt before network I/O.
        dispatch_possible = verified_reply = False
        sent = False

        def add_fact(operation, **facts):
            session.add(ShopOperationAudit(order_id=order_id, operator_id=attempt_operator_id,
                operation=operation, request_key=digest([operation, request_id]),
                facts={"request_id": request_id, **facts}, created_at=datetime.now(UTC)))

        async def record_fact(operation, **facts):
            await session.execute(select(Order.id).where(Order.id == order_id).with_for_update())
            add_fact(operation, **facts)
            await session.commit()

        def mark_sent():
            nonlocal sent
            sent = True

        async def persist_candidate(invoice_id):
            await record_fact("online_invoice_candidate", invoice_id=invoice_id)
        try:
            # Recheck all Shop flags and current operator immediately before dispatch.
            order = await sales.load_order(session, order_id)
            await guard(session, order, create=True)
            await session.rollback()

            async def before_dispatch():
                nonlocal dispatch_possible
                current = await authorize(session, order_id, customer, token, lock=True)
                current_actor, _ = await guard(session, current, create=True)
                payments.validate_order(current, dispatch_revision)
                if (not current.shop_online_payment_request or current.shop_online_payment_request.id != request_id
                        or current.shop_payment_intent.invoice_id is not None
                        or await selected_presentation(session, current.shop_online_payment_request) != (presentation, SHOP_INVOICE_VALIDITY)):
                    sales.fail("shop_online_request_conflict", "Recover the existing immutable checkout")
                session.add(ShopOperationAudit(order_id=order_id, operator_id=current_actor.id,
                    operation="online_dispatch_started", request_key=digest(["shop-online-dispatch", request_id]),
                    facts=dispatch_facts, created_at=datetime.now(UTC)))
                # Once this commits, no actor may assume that the POST did not happen.
                add_fact("online_invoice_dispatched")
                await session.commit()
                dispatch_possible = True
            proof = await provider.create_invoice(reference, amount, basket, validity=SHOP_INVOICE_VALIDITY,
                before_dispatch=before_dispatch, presentation=presentation, on_send=mark_sent, on_candidate=persist_candidate)
            request = await session.get(Request, request_id)
            creation_embed_url(proof, request, presentation, proof.get("invoice_id"))
            verified_reply = True
            order = await sales.load_order(session, order_id, lock=True)
            request = await session.get(Request, request_id)
            if order.status == OrderStatus.cancelled:
                if (not await order_archive_payments.is_owner_archived(session, order)
                        or not await order_archive_payments.dispatch_started(session, request)):
                    sales.fail("terminal_order", "Only a previously dispatched archived invoice can be attached")
                operator_id = request.operator_id
            else:
                await authorized_order(session, order, customer, token)
                if order.revision != dispatch_revision:
                    add_fact("online_invoice_finished")
                    await session.commit()
                    return await read(session, order_id, customer, token)
                actor, _ = await guard(session, order)
                operator_id = actor.id
            if await selected_presentation(session, request) != (presentation, SHOP_INVOICE_VALIDITY):
                sales.fail("shop_online_request_conflict", "The original checkout presentation is required")
            creation_embed_url(proof, request, presentation, proof.get("invoice_id"))
            binding = order.shop_payment_intent
            if binding.invoice_id and binding.invoice_id != proof["invoice_id"]:
                sales.fail("invoice_binding_conflict", "Recover the original invoice")
            binding.invoice_id = proof["invoice_id"]
            order.revision += 1
            session.add(ShopOperationAudit(order_id=order.id, operator_id=operator_id, operation="online_invoice_created",
                request_key=key, facts={"request_id":request.id,"proof":proof}, created_at=datetime.now(UTC)))
            add_fact("online_invoice_finished")
            await session.commit()
        except asyncio.CancelledError:
            await session.rollback()
            if dispatch_possible:
                await record_fact("online_invoice_finished")
            raise
        except Exception as error:
            if isinstance(error, MonobankError):
                error_reporting.provider_failure("invoice_create", error.code, order_id=order_id, attempt_id=request_id)
            else:
                error_reporting.report_exception("invoice_create", error, order_id=order_id, attempt_id=request_id)
            await session.rollback()
            if dispatch_possible:
                await record_fact("online_invoice_finished")
            if not sent:
                await record_fact("online_pre_dispatch_failed")
            await record_start_failure(session, order_id, request_id, sent, verified_reply, error)
            return await read(session, order_id, customer, token)
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


async def recovery_allowed(session, order):
    if order.status != OrderStatus.cancelled:
        return True
    return (await order_archive_payments.is_owner_archived(session, order)
        and not await session.scalar(select(func.shop_online_dispatch_pending(order.id))))


async def recover(session, actor, order_id, payload, raw_key):
    try:
        order = await sales.load_order(session, order_id)
        actor = await sales.current_actor(session, actor, "read", order)
        if not actor.is_superuser and not actor.can_manage_fiscal_settings:
            raise HTTPException(403, "Manager recovery permission required")
        actor_id = actor.id
        _, provider = await guard(session, order)
        request = order.__dict__.get("shop_online_payment_request")
        if not request or not await recovery_allowed(session, order):
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
        if (not await recovery_allowed(session, order) or order.revision != payload.expected_revision
                or order.shop_payment_intent.invoice_id):
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
