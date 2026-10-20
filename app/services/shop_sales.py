"""Explicit Shop money, physical handover and completion. No provider writes.

Payment verification and fiscal policy are server-owned seams. Their production
implementations deliberately remain unavailable until configured and reviewed.
"""
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderHandover, OrderIntent, OrderItem, OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentItem, TrackingEvent
from app.models.shop_sales import ShopCompletion, ShopFulfillmentFact, ShopOperationAudit, ShopPaymentConfirmation
from app.schemas.order import BackofficeOrderResponse
from app.schemas.shop_sales import ShopOperationResponse
from app.services import sales_access
from app.services.order import OrderService
from app.services.order_documents import claim_intent, digest, minor, operation_key


def fail(code, message, status=409):
    raise HTTPException(status, detail={"code": code, "message": message})


def aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC) if value else None


@dataclass(frozen=True)
class VerifiedPayment:
    reference: str
    order_id: int
    amount_minor: int
    currency: str
    method: str
    shipment_reference: str | None
    verification_id: str
    authority: str


@dataclass(frozen=True)
class FiscalAuthorization:
    order_id: int
    phase: str
    payment_id: int | None
    shipment_reference: str | None
    verification_id: str
    authority: str


class UnavailablePaymentAdapter:
    available = False
    test_only = False
    namespace = "unbound_provider"

    async def verify(self, reference, *, order_id, amount_minor, currency, shipment_reference):
        fail("provider_unavailable", "A production payment verification adapter is not configured")


class UnavailableFiscalPolicy:
    available = False
    test_only = False

    async def authorize(self, *, order_id, phase, payment_id, shipment_reference):
        fail("fiscal_policy_unavailable", "The fiscal policy for this operation is not configured")


class IsolatedTestPaymentAdapter:
    """Read-only fixture registry, never selected from client data or environment."""
    available = True
    test_only = True
    namespace = "isolated_test_provider"

    def __init__(self, records):
        self.records = records

    async def verify(self, reference, **expected):
        proof = self.records.get(reference)
        if proof is None:
            fail("provider_proof_invalid", "Payment is pending, unknown or not verified")
        return proof


class IsolatedTestFiscalPolicy:
    available = True
    test_only = True

    async def authorize(self, **expected):
        return FiscalAuthorization(**expected, verification_id="isolated-test-policy", authority="test")


payment_adapter = UnavailablePaymentAdapter()
fiscal_policy = UnavailableFiscalPolicy()


def effective_fiscal_policy(order=None):
    from app.services.shop_mono_checkbox import selected, DirectFiscalPolicy
    if selected(order):
        return DirectFiscalPolicy(order)
    # Local Shop B2 is an explicit test-only policy. A production policy remains
    # unavailable; existing fixture adapters continue to use their old contract.
    if isinstance(fiscal_policy, UnavailableFiscalPolicy):
        from app.services.shop_checkbox import fiscal_profile, CheckboxFiscalPolicy
        if fiscal_profile(order):
            return CheckboxFiscalPolicy(order)
    if isinstance(fiscal_policy, UnavailableFiscalPolicy) and settings.shop_fiscal_mock_enabled:
        from app.services.shop_fiscal import bound_policy
        return bound_policy
    return fiscal_policy


async def test_database_is_safe(session):
    bind = session.get_bind()
    url = bind.url
    if not (settings.app_env in {"local", "test"} and url.get_backend_name() == "postgresql"
            and url.host in {"localhost", "127.0.0.1", "::1"} and "test" in (url.database or "").lower()):
        return False
    actual = await session.scalar(text("SELECT current_database()"))
    return actual == url.database and "test" in actual.lower()


async def require_test_database(session, code):
    if not await test_database_is_safe(session):
        fail(code, "Connected database does not match the isolated test database")


async def current_actor(session, actor, operation, order):
    current = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if not current or not current.is_active:
        raise HTTPException(403, "Active Shop operator required")
    await sales_access.deny_master(session, current)
    fields = ["can_record_sales"]
    if operation in {"handover", "complete"}:
        fields.append("can_post_inventory")
        if order.fulfillment_method == "nova_poshta":
            fields.append("can_manage_shipments")
    if not current.is_superuser and not all(getattr(current, field, False) for field in fields):
        raise HTTPException(403, "Shop sales, inventory and applicable shipment permissions are required")
    return current


async def load_order(session, order_id, *, lock=False):
    if lock:
        await session.execute(select(OrderItem).where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id).with_for_update().execution_options(populate_existing=True))
    stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
    if lock:
        stmt = stmt.with_for_update()
    order = await session.scalar(stmt.execution_options(populate_existing=True))
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


def facts(order):
    return (order.__dict__.get("shop_payment_confirmation"),
            order.__dict__.get("shop_fulfillment_fact"), order.__dict__.get("shop_completion"))


def r2_test_context(order):
    binding = order.__dict__.get("shop_payment_intent")
    return bool(binding and (binding.authority == "test" or binding.profile.get("environment") == "test"))


def canonical_payment(order):
    confirmation, _, _ = facts(order)
    if not confirmation or len(order.payments) != 1 or order.channel != "shop" or order.simulation:
        return None
    payment = order.payments[0]
    snapshot = confirmation.snapshot
    binding = order.__dict__.get("shop_payment_intent")
    evidence = order.__dict__.get("shop_payment_evidence")
    if binding or confirmation.namespace.startswith("shop_r2_"):
        from app.services.shop_payments import canonical
        if (not binding or not evidence or evidence.intent_id != binding.id or evidence.order_id != order.id
                or evidence.payment_id != payment.id or evidence.operator_id != confirmation.operator_id
                or evidence.authority != confirmation.authority or evidence.proof_hash != digest(evidence.proof)
                or evidence.proof_canonical != canonical(evidence.proof) or binding.profile_hash != digest(binding.profile)
                or binding.amount_minor != minor(order.total_amount)
                or snapshot.get("verification") != {"r2_evidence_id":evidence.id,"intent_id":binding.id,"proof_hash":evidence.proof_hash}):
            return None
        if evidence.correction and (evidence.correction.intent_id!=binding.id or evidence.correction.original_invoice_id!=binding.invoice_id or evidence.correction.snapshot_hash!=digest(evidence.correction.snapshot)):
            return None
        proof = evidence.proof
        if proof.get("verified") is not True or proof.get("currency") != "UAH":
            return None
        if binding.mode == "prepayment":
            if (proof.get("status") != "success" or proof.get("invoice_id") != (evidence.correction.replacement_invoice_id if evidence.correction else binding.invoice_id)
                    or proof.get("reference") != binding.reference or proof.get("profile_id") != binding.profile_id
                    or proof.get("merchant_id") != binding.profile.get("merchant_id") or evidence.authority != binding.authority
                    or proof.get("authority") != binding.authority or proof.get("transport") != binding.transport
                    or type(proof.get("amount_minor")) is not int or proof["amount_minor"] != binding.amount_minor
                    or proof.get("proof_hash") != digest({k:v for k,v in proof.items() if k != "proof_hash"})):
                return None
        elif binding.mode == "cod":
            if (evidence.authority != "manual" or proof.get("issuer") != binding.profile.get("issuer")
                    or proof.get("account") != binding.profile.get("account") or proof.get("type") not in binding.profile.get("types",[])
                    or any(type(proof.get(k)) is not int for k in ("gross_amount_minor","fee_minor","net_amount_minor"))
                    or proof["gross_amount_minor"] != binding.amount_minor
                    or proof["gross_amount_minor"] != proof["fee_minor"] + proof["net_amount_minor"]):
                return None
        else:
            return None
    if (payment.simulation or confirmation.payment_id != payment.id or confirmation.order_id != order.id
            or confirmation.source not in {"manual", "provider"}
            or confirmation.authority not in ({"manual"} if confirmation.source == "manual" else {"provider", "test"})
            or confirmation.snapshot_hash != digest(snapshot)
            or snapshot.get("order_id") != order.id or snapshot.get("payment_id") != payment.id
            or snapshot.get("amount_minor") != minor(order.total_amount) or payment.amount_minor != minor(order.total_amount)
            or snapshot.get("currency") != "UAH" or snapshot.get("method") != payment.method
            or snapshot.get("authority") != confirmation.authority
            or snapshot.get("namespace") != confirmation.namespace
            or snapshot.get("external_reference") != confirmation.external_reference
            or snapshot.get("source") != confirmation.source
            or payment.source != "shop_" + confirmation.source
            or payment.evidence != confirmation.external_reference
            or payment.operator_id != confirmation.operator_id):
        return None
    return confirmation


def canonical_fulfillment(order):
    _, fulfillment, _ = facts(order)
    if not fulfillment or len(order.handovers) != 1 or order.channel != "shop" or order.simulation:
        return None
    handover = order.handovers[0]
    snapshot = fulfillment.snapshot
    if (handover.simulation or handover.id != fulfillment.handover_id
            or fulfillment.order_id != order.id or fulfillment.snapshot_hash != digest(snapshot)
            or snapshot.get("order_id") != order.id or snapshot.get("handover_id") != handover.id
            or snapshot.get("authority") != fulfillment.authority
            or snapshot.get("kind") != fulfillment.kind
            or snapshot.get("shipment_id") != fulfillment.shipment_id
            or snapshot.get("items") != [{"order_item_id": i.id, "product_id": i.product_id, "quantity": i.quantity}
                                         for i in sorted(order.items, key=lambda item: item.id)]
            or handover.operator_id != fulfillment.operator_id):
        return None
    return fulfillment


def canonical_completion(order):
    confirmation, fulfillment, completion = facts(order)
    if (not completion or not canonical_payment(order) or not canonical_fulfillment(order)
            or order.status != OrderStatus.completed or completion.order_id != order.id
            or completion.payment_confirmation_id != confirmation.id or completion.fulfillment_id != fulfillment.id
            or completion.snapshot_hash != digest(completion.snapshot)
            or completion.snapshot.get("order_id") != order.id
            or completion.snapshot.get("payment_confirmation_id") != confirmation.id
            or completion.snapshot.get("fulfillment_id") != fulfillment.id
            or completion.snapshot.get("authority") != completion.authority
            or completion.authority == "production" and (r2_test_context(order) or "test" in {confirmation.authority, fulfillment.authority})):
        return None
    return completion


def payment_projection(order):
    from app.services.shop_payment_resolutions import payment_projection as resolution_projection
    resolved=resolution_projection(order)
    if resolved:return resolved
    from app.services.shop_np_returns import cases as np_cases
    if any(c.financial_mode=="unpaid_cod" and c.__dict__.get("closure") for c in np_cases(order)):
        return "unpaid"
    confirmation = canonical_payment(order)
    if not confirmation:
        from app.services.shop_carrier_returns import projection
        if projection(order) == "simulated_unpaid_refusal_closed":
            return "unpaid"
    return ("simulated_paid" if confirmation.authority == "test" or r2_test_context(order) else "paid_" + confirmation.authority) if confirmation else None


def fulfillment_projection(order):
    from app.services.shop_payment_resolutions import fulfillment_projection as resolution_projection
    resolved=resolution_projection(order)
    if resolved:return resolved
    from app.services.shop_np_returns import projection as np_projection
    returned=np_projection(order)
    if returned:
        return returned
    fulfillment = canonical_fulfillment(order)
    if not fulfillment:
        return None
    from app.services.shop_carrier_returns import projection
    carrier_return = projection(order)
    if carrier_return:
        return carrier_return
    prefix = "simulated_" if fulfillment.authority == "test" or (facts(order)[2] and facts(order)[2].authority == "test") else ""
    completion = canonical_completion(order)
    return prefix + ("fulfilled" if completion and order.status == OrderStatus.completed else
        "carrier_dispatched" if fulfillment.kind == "nova_poshta" else "pickup_handed_over")


def eligible_order(order):
    return order.channel == "shop" and not order.simulation and order.fulfillment_method in {"pickup", "nova_poshta"}


def is_cod_order(order):
    return order.payment_method in {"cod", "cashOnDelivery", "cardOnDelivery", "cash_on_delivery", "card_on_delivery"}


async def real_shipment(session, order, *, delivered=False, lock=False):
    stmt = select(Shipment).where(Shipment.order_id == order.id).order_by(Shipment.id)
    if lock:
        stmt = stmt.with_for_update()
    rows = list((await session.scalars(stmt.execution_options(populate_existing=True))).all())
    from app.services.shop_carrier_returns import valid_binding
    outbound = []
    for row in rows:
        case = row.__dict__.get("shop_carrier_return_case")
        if case and valid_binding(case, order, row):
            continue
        outbound.append(row)
    rows = outbound
    if len(rows) != 1:
        fail("carrier_shipment_ambiguous" if rows else "carrier_shipment_required", "One full outbound shipment is required")
    shipment = rows[0]
    if (shipment.simulation or shipment.carrier != "nova_poshta" or shipment.source != "attach"
            or not shipment.active or shipment.return_of_shipment_id or not shipment.tracking_number
            or shipment.provider_reference != shipment.tracking_number):
        fail("carrier_shipment_required", "A matching real attached outbound Nova Poshta shipment is required")
    allocations = list((await session.scalars(select(ShipmentItem).where(ShipmentItem.shipment_id == shipment.id))).all())
    if {a.order_item_id: a.quantity for a in allocations} != {item.id: item.quantity for item in order.items}:
        fail("carrier_allocation_mismatch", "Shipment must cover every order item exactly")
    if shipment.tracking_status in {"refused", "returned", "returning", "cancelled", "requires_review"}:
        fail("tracking_ambiguous", "Carrier state requires reconciliation")
    if delivered:
        checked = aware(shipment.last_checked_at)
        if (shipment.last_attempt_status != "success" or not checked
                or checked < datetime.now(UTC) - timedelta(seconds=settings.np_tracking_stale_seconds)
                or checked > datetime.now(UTC) + timedelta(seconds=30)):
            fail("tracking_stale", "A fresh successful carrier check is required")
        if shipment.tracking_status != "delivered":
            fail("tracking_delivery_required", "Positive delivered tracking is required")
        event = await session.scalar(select(TrackingEvent).where(TrackingEvent.shipment_id == shipment.id,
            TrackingEvent.normalized_status == "delivered", TrackingEvent.observed_at <= shipment.last_checked_at,
            TrackingEvent.event_at == shipment.provider_event_at, TrackingEvent.raw_status == shipment.raw_status)
            .order_by(TrackingEvent.id.desc()))
        if not event or event.raw_status != shipment.raw_status or not aware(event.event_at):
            fail("tracking_delivery_required", "The successful check must have an accepted dated delivery event")
    return shipment


async def check_fiscal(session, order, phase, payment, shipment):
    policy = effective_fiscal_policy(order)
    from app.services.shop_np_returns import test_context as np_test_context
    if not policy.test_only and (np_test_context(order) or r2_test_context(order) or any(f and f.authority == "test" for f in facts(order))):
        fail("fiscal_policy_invalid", "Test authority cannot satisfy production fiscal policy")
    if policy.test_only:
        await require_test_database(session, "fiscal_test_unsafe")
    expected = dict(order_id=order.id, phase=phase,
        payment_id=payment.payment_id if payment else None,
        shipment_reference=shipment.tracking_number if shipment else None)
    proof = await policy.authorize_in_session(session, **expected) if hasattr(policy, "authorize_in_session") else await policy.authorize(**expected)
    if (not isinstance(proof, FiscalAuthorization) or proof.order_id != order.id or proof.phase != phase
            or proof.payment_id != (payment.payment_id if payment else None)
            or proof.shipment_reference != (shipment.tracking_number if shipment else None)
            or not proof.verification_id or proof.authority != ("test" if policy.test_only else "production")):
        fail("fiscal_policy_invalid", "Fiscal policy did not authorize this exact operation")
    return proof


async def response(session, order, actor):
    from app.services.shop_checkbox import profile_metadata
    from app.services.shop_payments import selected
    r2 = await selected(session, order.id)
    from app.services.shop_np_returns import active_case as np_active, test_context as np_test_context
    policy = effective_fiscal_policy(order)
    confirmation = canonical_payment(order)
    fulfillment = canonical_fulfillment(order)
    completion = canonical_completion(order)
    from app.services.shop_payment_resolutions import test_context as resolution_test,active as resolution_active
    has_test = resolution_test(order) or np_test_context(order) or r2_test_context(order) or any(f and f.authority == "test" for f in (confirmation, fulfillment, completion))
    reasons = []
    if not settings.shop_order_operations_enabled:
        reasons.append("shop_operations_disabled")
    if not eligible_order(order):
        reasons.append("shop_order_required")
    test_safe = await test_database_is_safe(session) if payment_adapter.test_only or policy.test_only else False
    fiscal_available = policy.available and (not policy.test_only or test_safe)
    if (resolution_test(order) or np_test_context(order) or r2_test_context(order)) and not policy.test_only:
        fiscal_available = False
    provider_available = payment_adapter.available and (not payment_adapter.test_only or test_safe)
    if not fiscal_available:
        reasons.append("fiscal_policy_unavailable")
    if not provider_available:
        reasons.append("provider_unavailable")
    positive = minor(order.total_amount) > 0
    if not positive:
        reasons.append("unsupported_zero_total")
    incompatible = bool((order.payments and not confirmation) or (order.handovers and not fulfillment) or order.qr_payment_attempts)
    if incompatible:
        reasons.append("incompatible_sales_facts")
    enabled = settings.shop_order_operations_enabled and eligible_order(order) and positive and not incompatible and order.status not in {OrderStatus.cancelled, OrderStatus.completed}
    from app.services.shop_carrier_returns import active_case
    if resolution_active(order):
        enabled=False
        reasons.append("resolution_active")
    if active_case(order) or np_active(order):
        enabled = False
        reasons.append("carrier_return_active")
    money_permission = actor.is_superuser or actor.can_record_sales
    payment_methods = ["cod"] if is_cod_order(order) else ["cash", "card", "bank_transfer"]
    if hasattr(policy, "payment_methods"):
        payment_methods, payment_reasons = policy.payment_methods(order)
        reasons.extend(code for code in payment_reasons if code not in reasons)
        if not confirmation:
            admission_reasons = await policy.money_readiness(session, order)
            reasons.extend(code for code in admission_reasons if code not in reasons)
            if admission_reasons:
                payment_methods = []
    physical_permission = actor.is_superuser or (actor.can_record_sales and actor.can_post_inventory
        and (order.fulfillment_method != "nova_poshta" or actor.can_manage_shipments))
    payment = next((p for p in order.payments if confirmation and p.id == confirmation.payment_id), None)
    shipment = next((s for s in order.shipments if fulfillment and s.id == fulfillment.shipment_id), None)
    dispatch_reasons = await policy.readiness(session, order, "dispatch") if hasattr(policy, "readiness") else []
    complete_reasons = await policy.readiness(session, order, "complete") if hasattr(policy, "readiness") else []
    reasons.extend(code for code in dispatch_reasons + complete_reasons if code not in reasons)
    return ShopOperationResponse.model_validate({
        "fiscal_profile": profile_metadata(order),
        "order_id": order.id, "revision": order.revision,
        "order": BackofficeOrderResponse.model_validate(order),
        "stage": "completed" if completion else "dispatched" if fulfillment else "paid" if confirmation else "reserved",
        "authority": "test" if has_test else confirmation.authority if confirmation else "none",
        "payment": {"id": confirmation.id, "method": payment.method, "source": confirmation.source,
            "amount_minor": payment.amount_minor, "external_reference": confirmation.external_reference,
            "authority": confirmation.authority, "created_at": confirmation.created_at} if confirmation else None,
        "fulfillment": {"id": fulfillment.id, "kind": fulfillment.kind, "shipment_id": fulfillment.shipment_id,
            "tracking_number": shipment.tracking_number if shipment else None,
            "authority": fulfillment.authority, "created_at": fulfillment.created_at} if fulfillment else None,
        "completion": {"id": completion.id, "authority": completion.authority, "created_at": completion.created_at} if completion else None,
        "payments_profile": "r2" if r2 else "legacy",
        "actions": {"record_manual_payment": bool(not r2 and enabled and money_permission and not order.payments and payment_methods),
            "record_provider_payment": bool(not r2 and enabled and money_permission and not order.payments and provider_available and payment_methods),
            "handover": bool(enabled and physical_permission and not order.handovers and fiscal_available and not dispatch_reasons
                and (confirmation or order.fulfillment_method == "nova_poshta")),
            "complete": bool(enabled and physical_permission and confirmation and fulfillment and not completion and fiscal_available and not complete_reasons),
            "replay_manual_payment": bool(eligible_order(order) and money_permission),
            "replay_provider_payment": bool(eligible_order(order) and money_permission),
            "replay_handover": bool(eligible_order(order) and physical_permission),
            "replay_complete": bool(eligible_order(order) and physical_permission)},
        "blocking_reasons": reasons,
        "available_payment_methods": payment_methods,
    })


async def read(session, actor, order_id):
    order = await load_order(session, order_id)
    actor = await current_actor(session, actor, "read", order)
    return await response(session, order, actor)


async def command(session, actor, order_id, payload, raw_key, operation):
    try:
        order = await load_order(session, order_id)
        actor = await current_actor(session, actor, operation, order)
        from app.services.shop_np_returns import active_case as np_active, test_context as np_test_context
        if np_test_context(order):
            await require_test_database(session,"np_return_test_unsafe")
        if r2_test_context(order):
            await require_test_database(session, "payment_test_unsafe")
        actor_id = actor.id
        key = operation_key(raw_key, f"shop-{operation}:{actor_id}:{order_id}")
        request_hash = digest(payload.model_dump(mode="json"))
        previous = await session.get(OrderIntent, key)
        if previous:
            if previous.payload_hash != request_hash:
                fail("idempotency_conflict", "Key already used for another request")
            if previous.response_json:
                return ShopOperationResponse.model_validate(previous.response_json)
        from app.services.shop_payment_resolutions import active as resolution_active,test_context as resolution_test
        if resolution_test(order):await require_test_database(session,"resolution_test_unsafe")
        if resolution_active(order):fail("resolution_active","Use the canonical payment resolution")
        from app.services.shop_payments import selected
        if operation in {"manual_payment", "provider_payment"} and await selected(session, order.id):
            fail("shop_payments_workflow_required", "Use Monobank invoice verification or structured manual COD settlement")
        if not settings.shop_order_operations_enabled:
            fail("shop_operations_disabled", "Shop order operations are closed", 503)
        if not eligible_order(order):
            fail("shop_order_required", "A non-simulated pickup or Nova Poshta Shop order is required")
        if minor(order.total_amount) <= 0:
            fail("unsupported_zero_total", "Shop operations require a positive server order total")
        from app.services.shop_carrier_returns import active_case
        if active_case(order) or np_active(order):
            fail("carrier_return_active", "Use the canonical carrier return workflow")
        proof = None
        if operation == "provider_payment":
            shipment = await real_shipment(session, order) if order.fulfillment_method == "nova_poshta" else None
            expected = {"order_id": order.id, "amount_minor": minor(order.total_amount), "currency": "UAH",
                        "shipment_reference": shipment.tracking_number if shipment else None}
            if payment_adapter.test_only:
                await require_test_database(session, "provider_test_unsafe")
            await session.rollback()  # no business row lock is held during provider verification
            proof = await payment_adapter.verify(payload.reference, **expected)
            actor = await current_actor(session, SimpleNamespace(id=actor_id), operation,
                await load_order(session, order_id))
        order = await load_order(session, order_id, lock=True)
        from app.services.shop_payment_resolutions import active as resolution_active
        if resolution_active(order):fail("resolution_active","Use the canonical payment resolution")
        intent = await claim_intent(session, key, request_hash)
        if intent.response_json:
            return ShopOperationResponse.model_validate(intent.response_json)
        if not eligible_order(order):
            fail("shop_order_required", "A non-simulated pickup or Nova Poshta Shop order is required")
        if minor(order.total_amount) <= 0:
            fail("unsupported_zero_total", "Shop operations require a positive server order total")
        confirmation, fulfillment, completion = facts(order)
        if active_case(order) or np_active(order):
            fail("carrier_return_active", "Use the canonical carrier return workflow")
        if any(f and f.authority == "test" for f in (confirmation, fulfillment, completion)):
            await require_test_database(session, "provider_test_unsafe")
        binding = order.__dict__.get("shop_payment_intent")
        if binding and (binding.authority == "test" or binding.profile.get("environment") == "test"):
            await require_test_database(session, "payment_test_unsafe")
        if order.revision != payload.expected_revision:
            fail("revision_conflict", "Reload order")
        if order.status in {OrderStatus.cancelled, OrderStatus.completed} or completion:
            fail("terminal_order", "Order is already closed")
        if (order.payments and not canonical_payment(order)) or (order.handovers and not canonical_fulfillment(order)) or order.qr_payment_attempts:
            fail("incompatible_sales_facts", "Existing legacy, mock or QR facts require their own workflow")
        now = datetime.now(UTC)
        if operation in {"manual_payment", "provider_payment"}:
            if order.payments:
                fail("payment_exists", "Payment is already recorded")
            shipment = await real_shipment(session, order, lock=True) if order.fulfillment_method == "nova_poshta" else None
            if operation == "manual_payment":
                method, reference, namespace, authority = payload.method, payload.external_reference, "shop_manual", "manual"
                if payload.amount_minor != minor(order.total_amount):
                    fail("amount_mismatch", "Payment must equal the server order total")
                verification = {"evidence": payload.evidence}
                source = "manual"
            else:
                if (not isinstance(proof, VerifiedPayment) or proof.reference != payload.reference
                        or proof.order_id != order.id or type(proof.amount_minor) is not int
                        or proof.amount_minor != minor(order.total_amount) or proof.currency != "UAH"
                        or proof.shipment_reference != (shipment.tracking_number if shipment else None)
                        or proof.method not in {"cash", "card", "bank_transfer", "cod"}
                        or not proof.verification_id or proof.authority != ("test" if payment_adapter.test_only else "provider")):
                    fail("provider_proof_invalid", "Provider did not verify this exact payment and order")
                if payment_adapter.test_only:
                    await require_test_database(session, "provider_test_unsafe")
                method, reference, namespace, authority = proof.method, proof.reference, payment_adapter.namespace, proof.authority
                verification, source = asdict(proof), "provider"
            if method == "cod":
                if not shipment:
                    fail("carrier_shipment_required", "COD requires Nova Poshta delivery")
                await real_shipment(session, order, delivered=True, lock=True)
                if not canonical_fulfillment(order):
                    fail("handover_required", "Record carrier dispatch before COD payment")
            elif is_cod_order(order):
                fail("provider_proof_invalid", "A COD order requires an explicit COD payment confirmation")
            await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"shop-payment:{namespace}:{reference}", 0))))
            if await session.scalar(select(ShopPaymentConfirmation.id).where(
                    ShopPaymentConfirmation.namespace == namespace, ShopPaymentConfirmation.external_reference == reference)):
                fail("external_reference_used", "This payment reference is already assigned to an order")
            from app.services.shop_fiscal import payment_barrier
            from app.services.shop_checkbox import payment_barrier as checkbox_payment_barrier
            await checkbox_payment_barrier(session, order, method, minor(order.total_amount))
            await payment_barrier(session)
            payment = OrderPayment(order_id=order.id, amount_minor=minor(order.total_amount), method=method,
                source="shop_" + source, evidence=reference, operator_id=actor.id, simulation=False, created_at=now)
            session.add(payment)
            await session.flush()
            snapshot = {"order_id": order.id, "payment_id": payment.id, "amount_minor": payment.amount_minor,
                "currency": "UAH", "method": method, "namespace": namespace, "external_reference": reference,
                "source": source, "authority": authority, "verification": verification,
                "original_payment_method": order.payment_method}
            confirmation = ShopPaymentConfirmation(order_id=order.id, payment_id=payment.id, namespace=namespace,
                external_reference=reference, source=source, authority=authority, snapshot=snapshot,
                snapshot_hash=digest(snapshot), operator_id=actor.id, created_at=now)
            session.add(confirmation)
            order.payment_method, order.status = method, OrderStatus.paid
        elif operation == "handover":
            if order.handovers or fulfillment:
                fail("handover_exists", "Physical handover is already recorded")
            confirmation = canonical_payment(order)
            shipment = await real_shipment(session, order, lock=True) if order.fulfillment_method == "nova_poshta" else None
            if not shipment and not confirmation:
                fail("payment_required", "Pickup requires a canonical payment")
            if shipment and not confirmation and not is_cod_order(order):
                fail("payment_required", "Unpaid carrier dispatch is supported only for COD")
            fiscal = await check_fiscal(session, order, "dispatch", confirmation, shipment)
            products = {p.id: p for p in (await session.scalars(select(Product)
                .where(Product.id.in_(sorted({i.product_id for i in order.items})))
                .order_by(Product.id).with_for_update().execution_options(populate_existing=True))).all()}
            try:
                await OrderService.validate_owned_reservations(session, order, order.items)
            except HTTPException as error:
                if error.status_code == 409:
                    fail("reservation_ownership_mismatch", "Owned reservation ledger requires reconciliation")
                raise
            try:
                OrderService.apply_handover_inventory(session, order, order.items, products, actor.id)
            except HTTPException as error:
                if error.status_code == 409:
                    fail("procurement_required" if any(i.quantity_from_stock + i.quantity_received_for_order != i.quantity for i in order.items)
                         else "reservation_ownership_mismatch", "Full owned stock is required for handover")
                raise
            handover = OrderHandover(order_id=order.id, operator_id=actor.id, evidence=payload.evidence,
                simulation=False, created_at=now)
            session.add(handover)
            await session.flush()
            snapshot = {"order_id": order.id, "handover_id": handover.id, "kind": order.fulfillment_method,
                "shipment_id": shipment.id if shipment else None, "authority": fiscal.authority,
                "fiscal_policy": asdict(fiscal), "evidence": payload.evidence,
                "items": [{"order_item_id": i.id, "product_id": i.product_id, "quantity": i.quantity}
                          for i in sorted(order.items, key=lambda item: item.id)]}
            fulfillment = ShopFulfillmentFact(order_id=order.id, handover_id=handover.id,
                shipment_id=shipment.id if shipment else None, kind=order.fulfillment_method,
                authority=fiscal.authority, snapshot=snapshot, snapshot_hash=digest(snapshot),
                operator_id=actor.id, created_at=now)
            session.add(fulfillment)
            if shipment:
                shipment.handed_over_at = now
        elif operation == "complete":
            confirmation, fulfillment = canonical_payment(order), canonical_fulfillment(order)
            if not confirmation:
                fail("payment_required", "Completion requires a canonical full payment")
            if not fulfillment:
                fail("handover_required", "Completion requires recorded physical handover")
            shipment = await real_shipment(session, order, delivered=True, lock=True) if fulfillment.kind == "nova_poshta" else None
            if shipment and shipment.id != fulfillment.shipment_id:
                fail("carrier_shipment_required", "Completion must match the dispatched shipment")
            fiscal = await check_fiscal(session, order, "complete", confirmation, shipment)
            authority = "test" if "test" in {fiscal.authority, confirmation.authority, fulfillment.authority} else "production"
            snapshot = {"order_id": order.id, "payment_confirmation_id": confirmation.id,
                "fulfillment_id": fulfillment.id, "authority": authority, "fiscal_policy": asdict(fiscal), "evidence": payload.evidence}
            completion = ShopCompletion(order_id=order.id, payment_confirmation_id=confirmation.id,
                fulfillment_id=fulfillment.id, authority=authority, snapshot=snapshot, snapshot_hash=digest(snapshot),
                operator_id=actor.id, created_at=now)
            session.add(completion)
            order.status = OrderStatus.completed
        else:
            raise ValueError("Unsupported Shop operation")
        order.revision += 1
        session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id, operation=operation,
            request_key=key, facts={"revision": order.revision}, created_at=now))
        await session.flush()
        order = await load_order(session, order.id)
        result = await response(session, order, actor)
        intent.order_id, intent.response_json = order.id, result.model_dump(mode="json")
        await session.commit()
        return result
    except IntegrityError as error:
        await session.rollback()
        constraint = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
        if constraint == "uq_shop_payment_external_reference":
            fail("external_reference_used", "This payment reference is already assigned to an order")
        raise
    except Exception:
        await session.rollback()
        raise
