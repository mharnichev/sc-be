"""Canonical Checkbox orchestration; all transport and proof verification is server-owned.

Database fences are committed before provider mutations. An uncertain mutation
is only looked up; an empty lookup never permits another create.
"""
from datetime import UTC, datetime, timedelta
from html import escape
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import OrderIntent
from app.models.product import Product
from app.models.shop_checkbox import (ShopCheckboxRegister as Register, ShopCheckboxBinding as Binding,
    ShopCheckboxOperation as Operation, ShopCheckboxAcceptance as Acceptance, ShopCheckboxPreview as Preview,
    ShopCheckboxDocument as Document, ShopCheckboxReturn as FinancialReturn, ShopCheckboxRefundFact as Refund,
    ShopCheckboxPhysicalReceipt as PhysicalReceipt, ShopCheckboxAudit as Audit)
from app.models.shop_sales import ShopPaymentConfirmation
from app.schemas import shop_checkbox as dto
from app.services import shop_sales, shop_fiscal, sales_access
from app.services.fiscal_returns import net_lines
from app.services.order_documents import claim_intent, operation_key, digest

fail, aware = shop_sales.fail, shop_sales.aware
PENDING = {"queued", "unknown", "pending"}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def gateway(profile=None):
    from app.services.checkbox_provider import get_gateway
    return get_gateway(profile)


def permission(kind):
    return "can_fiscalize_sales" if kind == "sale" else "can_fiscalize_returns" if kind == "return" else "can_manage_fiscal_settings"


async def actor_for(session, actor, field=None):
    actor = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if actor:
        await sales_access.deny_master(session, actor)
    if not shop_fiscal.permitted(actor, field):
        raise HTTPException(403, "Current Checkbox permission required")
    return actor


async def safe(session, provider):
    if provider.test_only:
        await shop_sales.require_test_database(session, "checkbox_test_unsafe")


async def guard(session, provider, *, recovery=False):
    if not recovery and not settings.shop_checkbox_enabled:
        fail("shop_checkbox_disabled", "Shop Checkbox operations are disabled", 503)
    if not provider.available:
        fail("checkbox_profile_unconfigured", "An approved Checkbox profile and transport are required")
    await safe(session, provider)


async def is_enabled(session, provider):
    return bool(settings.shop_checkbox_enabled and provider.available
        and (not provider.test_only or await shop_sales.test_database_is_safe(session)))


async def register(session, provider=None, *, lock=False, create=False, register_id=None):
    if register_id is None:
        register_id = (provider.profile or {}).get("id") if provider else None
    if not register_id:
        return None
    if create:
        try:
            await session.execute(insert(Register).values(id=register_id, profile=provider.profile,
                profile_hash=digest(provider.profile), authority="test" if provider.test_only else "provider",
                transport=provider.transport, revision=1, shift_status="closed").on_conflict_do_nothing(index_elements=["id"]))
        except IntegrityError:
            await session.rollback()
            fail("checkbox_profile_conflict", "This physical cash register already belongs to its immutable profile")
    query = select(Register).where(Register.id == register_id).execution_options(populate_existing=True)
    row = await session.scalar(query.with_for_update() if lock else query)
    if row and provider and (lock or create) and (row.profile_hash != digest(provider.profile)
            or row.authority != ("test" if provider.test_only else "provider") or row.transport != provider.transport):
        fail("checkbox_profile_conflict", "Restore the immutable register profile and transport")
    return row


async def pending(session, row):
    return await session.scalar(select(Operation).where(Operation.register_id == row.id,
        Operation.status.in_(PENDING)).order_by(Operation.id))


async def free_register(session, row, *, except_id=None):
    blocking = await pending(session, row)
    if blocking and blocking.id != except_id:
        fail("checkbox_register_busy", "Resolve the existing register operation first")


def identity_valid(row):
    proof = row.identity if row else None
    return bool(proof and proof.get("verified") is True and proof.get("authority") == row.authority
        and proof.get("transport") == row.transport and all(proof.get(k) == row.profile.get(k)
            for k in ("organization_id", "register_id", "cashier_id")))


def open_register(row):
    if not identity_valid(row):
        fail("checkbox_identity_required", "Verify the configured fiscal identities")
    if row.shift_status != "open" or not row.shift_id:
        fail("checkbox_shift_closed", "A verified open Checkbox shift is required")


def revision(row, expected):
    if row.revision != expected:
        fail("revision_conflict", "Reload the current resource")


def audit(session, actor, op, **facts):
    row = Audit(operator_id=actor.id, operation=op, facts=facts, created_at=datetime.now(UTC))
    session.add(row)
    return row


def key_for(actor, row, scope, key):
    return operation_key(key, f"shop-checkbox:{actor.id}:{row.id}:{row.profile_hash}:{scope}")


async def cached(session, actor, row, scope, key, payload, view):
    command = await session.get(OrderIntent, key_for(actor, row, scope, key))
    if command and command.payload_hash != digest(payload.model_dump(mode="json")):
        fail("idempotency_conflict", "The key already belongs to another request")
    return view.model_validate(command.response_json) if command and command.response_json else None


async def intent(session, actor, row, scope, key, payload):
    return await claim_intent(session, key_for(actor, row, scope, key), digest(payload.model_dump(mode="json")))


async def finish(session, actor, command, result, op, order_id=None):
    command.order_id, command.response_json = order_id, result.model_dump(mode="json")
    audit(session, actor, op, order_id=order_id, result_id=getattr(result, "id", None))
    await session.commit()
    return result


async def binding(session, order, row, *, create=False):
    bound = await session.get(Binding, order.id)
    if bound and (bound.register_id != row.id or bound.profile_hash != row.profile_hash or bound.authority != row.authority):
        fail("checkbox_profile_conflict", "The order belongs to a different immutable fiscal profile")
    if order.shop_fiscal_documents or order.shop_financial_returns or order.shop_carrier_returns:
        fail("checkbox_profile_conflict", "Local B2/B3 history cannot become Checkbox proof")
    if not bound and any(shop_sales.facts(order)):
        fail("checkbox_history_unbound", "Historical Shop money or handover lacks an original Checkbox binding; explicit future reconciliation is required")
    from app.models.shop_payments import ShopPaymentIntent
    payment_intent = await session.scalar(select(ShopPaymentIntent).where(ShopPaymentIntent.order_id == order.id))
    r2_test = shop_sales.r2_test_context(order) or bool(payment_intent and (
        payment_intent.authority == "test" or payment_intent.profile.get("environment") == "test"))
    from app.models.shop_np_returns import ShopNPReturnCase
    np_test = await session.scalar(select(ShopNPReturnCase.id).where(ShopNPReturnCase.order_id==order.id,ShopNPReturnCase.test_context.is_(True)))
    from app.models.shop_payment_resolutions import ShopPaymentResolutionCase, ShopPaymentBankClosure
    r4_test = await session.scalar(select(ShopPaymentResolutionCase.id).outerjoin(ShopPaymentBankClosure, ShopPaymentBankClosure.case_id==ShopPaymentResolutionCase.id).where(ShopPaymentResolutionCase.order_id==order.id, (ShopPaymentResolutionCase.test_context.is_(True) | (ShopPaymentBankClosure.policy["environment"].as_string()=="test"))))
    if (r4_test or np_test or r2_test or any(f and f.authority == "test" for f in shop_sales.facts(order))) and row.authority != "test":
        fail("checkbox_profile_conflict", "Test Shop facts cannot become production proof")
    if not bound and create:
        bound = Binding(order_id=order.id, register_id=row.id, profile_hash=row.profile_hash,
            authority=row.authority, created_at=datetime.now(UTC))
        session.add(bound)
        await session.flush()
    return bound


async def payment_barrier(session, order, method=None, amount_minor=None):
    """Called while B1 holds item/order locks, before admitting a new money fact."""
    bound = await session.get(Binding, order.id)
    provider = gateway()
    if not bound and profile_metadata(order) != "checkbox":
        return
    if bound:
        row = await register(session, lock=True, register_id=bound.register_id)
        provider = gateway(row.profile)
        await guard(session, provider)
        await register(session, provider, lock=True)
    else:
        await guard(session, provider)
        row = await register(session, provider, lock=True, create=True)
    await free_register(session, row)
    open_register(row)
    preflight_payment(order, row.profile, method or ("cod" if shop_sales.is_cod_order(order) else order.payment_method), amount_minor)
    await binding(session, order, row, create=True)


def truth_valid(operation):
    truth = operation.__dict__.get("acceptance")
    valid = bool(operation.status == "accepted" and truth and truth.operation_id == operation.id
        and truth.snapshot_hash == operation.snapshot_hash and digest(operation.snapshot) == operation.snapshot_hash
        and digest(truth.proof) == truth.proof_hash and truth.proof.get("verified") is True
        and truth.proof.get("accepted") is True and truth.proof.get("authority") == operation.authority
        and truth.proof.get("transport") == operation.transport
        and truth.proof_canonical == canonical_json(truth.proof)
        and all(truth.proof.get(k) == operation.snapshot.get(k) for k in ("organization_id", "register_id", "cashier_id")))
    if not valid:
        return False
    proof = truth.proof
    if operation.kind in {"sale", "return"}:
        return bool(proof.get("resource") == "receipt" and proof.get("status") == "DONE"
            and proof.get("id") == operation.provider_uuid and proof.get("kind") == operation.kind
            and proof.get("snapshot_hash") == operation.snapshot_hash and proof.get("shift_id") == operation.shift_id
            and proof.get("amount_minor") == operation.snapshot.get("amount_minor") and proof.get("currency") == "UAH"
            and proof.get("related_receipt_id") == operation.snapshot.get("related_receipt_id")
            and proof.get("fiscal_code") and proof.get("is_sent_dps") is (operation.authority == "provider"))
    return True


async def load_operation(session, operation_id, *, lock=False):
    stmt = select(Operation).where(Operation.id == operation_id).execution_options(populate_existing=True)
    op = await session.scalar(stmt.with_for_update() if lock else stmt)
    if not op:
        raise HTTPException(404, "Checkbox operation not found")
    return op


async def load_document(session, identifier):
    doc = await session.get(Document, identifier, populate_existing=True)
    if not doc:
        raise HTTPException(404, "Checkbox document not found")
    return doc


async def issued(session, doc):
    if not doc:
        fail("sale_receipt_required", "A canonical issued Checkbox receipt is required")
    op = await load_operation(session, doc.operation_id)
    if not truth_valid(op) or op.kind != doc.kind or op.order_id != doc.order_id:
        fail("sale_receipt_required", "Receipt lacks its exact durable verified acceptance")
    return op


async def sale_for(session, order_id):
    return await session.scalar(select(Document).join(Operation, Operation.id == Document.operation_id)
        .where(Document.order_id == order_id, Document.kind == "sale", Operation.status.notin_(["cancelled", "rejected"]))
        .order_by(Document.id.desc()))


def tender(profile, order, confirmation):
    method = order.payment_method
    if confirmation:
        original = confirmation.snapshot.get("original_payment_method")
        method = original if confirmation.snapshot["method"] == "cod" else confirmation.snapshot["method"]
    result = profile.get("tenders", {}).get(method)
    if not result or not all(k in result for k in ("type", "label", "code")):
        fail("checkbox_payment_mapping_invalid", "An explicit tender mapping for this original payment method is required")
    return dict(result)


def canonical_order(order):
    if not shop_sales.eligible_order(order):
        fail("shop_order_required", "A canonical non-simulated Shop order is required")
    confirmation = shop_sales.canonical_payment(order)
    if ((order.payments and not confirmation) or (order.handovers and not shop_sales.canonical_fulfillment(order))
            or order.qr_payment_attempts):
        fail("incompatible_sales_facts", "Legacy, simulated or QR facts cannot become Checkbox facts")
    return confirmation


def tax_items(profile, items):
    result = []
    for item in items:
        codes = profile.get("product_tax_codes", {}).get(str(item["product_id"]))
        if codes is None or any(str(c) not in profile.get("taxes", {}) for c in codes):
            fail("checkbox_tax_profile_invalid", "Every item requires an explicitly approved tax mapping")
        unit = item.get("unit_price_minor")
        if unit is None:
            fail("invalid_original_amounts", "The original captured unit price is required")
        result.append({**item, "unit_price_minor": unit, "tax_codes": codes})
    return result


def sale_inputs(order, profile, method=None):
    items = [{"order_item_id": i.id, "product_id": i.product_id, "name": i.product_name,
        "quantity": i.quantity, "unit_price_minor": shop_fiscal.exact_minor(i.price),
        "total_minor": shop_fiscal.exact_minor(i.total_price if i.total_price is not None else i.price * i.quantity)}
        for i in sorted(order.items, key=lambda i: i.id)]
    amount = shop_fiscal.exact_minor(order.total_amount)
    state = {"items": items, "amount_minor": amount, "additional_discount_minor": sum(i["total_minor"] for i in items) - amount}
    if amount <= 0 or state["additional_discount_minor"] < 0:
        fail("unsupported_zero_total", "Exact positive canonical server totals are required")
    allocated = net_lines(state)
    items = tax_items(profile, [{**i, "total_minor": allocated[i["order_item_id"]]} for i in items])
    confirmation = shop_sales.canonical_payment(order)
    if method is None:
        payment = tender(profile, order, confirmation)
    else:
        original_method = (confirmation.snapshot.get("original_payment_method") if confirmation else order.payment_method) if method == "cod" else method
        payment = profile.get("tenders", {}).get(original_method)
        if not payment:
            fail("checkbox_payment_mapping_invalid", "An explicit mapping for this exact original tender is required")
    return items, dict(payment, value=amount), amount


def preflight_payment(order, profile, method, amount_minor=None):
    from app.services.checkbox_provider import build_receipt_payload, CheckboxProviderError
    items, payment, amount = sale_inputs(order, profile, method)
    if amount_minor is not None and amount_minor != amount:
        fail("amount_mismatch", "Payment must equal the exact server order amount")
    # Pure serializer validation only. These temporary IDs are never persisted or sent.
    try:
        build_receipt_payload(str(uuid4()), {"kind": "sale", "shift_id": str(uuid4()), "amount_minor": amount,
            "currency": "UAH", "items": items, "payment": payment, "related_receipt_id": None}, profile)
    except CheckboxProviderError as error:
        fail(error.code, "The approved fiscal profile cannot represent this order before accepting money or dispatch")


async def snapshot(session, order, row, financial_return=None):
    confirmation = canonical_order(order)
    if not confirmation:
        fail("payment_required", "A canonical full B1 payment is required")
    await binding(session, order, row)
    if financial_return:
        original = await load_document(session, financial_return.sale_document_id)
        original_op = await issued(session, original)
        refund = await session.scalar(select(Refund).where(Refund.return_id == financial_return.id))
        physical = await physical_receipt_for(session, financial_return)
        if not refund:
            fail("refund_required", "Record the separate explicit staff refund fact first")
        from app.services.shop_payment_resolutions import cancellation_valid
        if not physical and not await cancellation_valid(session, financial_return):
            fail("physical_receipt_required", "Record the exact pickup physical receipt first")
        if financial_return.shift_id != row.shift_id:
            fail("checkbox_shift_unknown", "The refund obligation belongs to its original shift")
        items, amount = financial_return.lines, financial_return.amount_minor
        payment = dict(original_op.snapshot["payment"], value=amount)
        related = original_op.provider_uuid
    else:
        items, payment, amount = sale_inputs(order, row.profile)
        related = None
    return {"document_kind": "shop_checkbox_receipt", "kind": "return" if financial_return else "sale",
        "authority": row.authority, "transport": row.transport, "profile_id": row.id, "profile_hash": row.profile_hash,
        "organization_id": row.profile["organization_id"], "register_id": row.profile["register_id"],
        "cashier_id": row.profile["cashier_id"], "shift_id": row.shift_id,
        "order_id": order.id, "payment_confirmation_id": confirmation.id,
        "return_id": financial_return.id if financial_return else None, "amount_minor": amount, "currency": "UAH",
        "additional_discount_minor": 0,
        "items": tax_items(row.profile, items), "payment": payment, "related_receipt_id": related,
        "tax_profile": row.profile["taxes"]}


async def reconciliation(session, row):
    # Register owner never locks orders: immutable bindings/facts serialize admission.
    from app.services.shop_payment_resolutions import cancellation_valid
    operations = list((await session.scalars(select(Operation).where(Operation.register_id == row.id))).all())
    documents = list((await session.scalars(select(Document).join(Operation, Operation.id == Document.operation_id)
        .where(Operation.register_id == row.id))).all())
    accepted = {o.id for o in operations if truth_valid(o)}
    sales = {d.payment_confirmation_id for d in documents if d.kind == "sale" and d.operation_id in accepted}
    returns = {d.return_id for d in documents if d.kind == "return" and d.operation_id in accepted}
    payments = list((await session.scalars(select(ShopPaymentConfirmation).join(Binding,
        Binding.order_id == ShopPaymentConfirmation.order_id).where(Binding.register_id == row.id))).all())
    obligations = list((await session.scalars(select(FinancialReturn).where(FinancialReturn.register_id == row.id))).all())
    unresolved = []
    for item in obligations:
        if (item.id not in returns or not await session.scalar(select(Refund.id).where(Refund.return_id == item.id))
                or not (await physical_receipt_for(session, item) or await cancellation_valid(session,item))):
            unresolved.append(item.id)
    from app.services.shop_payment_resolutions import unresolved as unresolved_r4
    from app.services.shop_np_returns import unresolved as unresolved_np
    return {"pending_operation_ids": [o.id for o in operations if o.status in PENDING],
        "unreceipted_payment_ids": [p.id for p in payments if p.id not in sales], "unresolved_return_ids": unresolved,
        "unresolved_np_return_ids": await unresolved_np(session,row.id), "unresolved_payment_resolution_ids":await unresolved_r4(session,row.id)}


async def operation_view(session, actor, op):
    row = await register(session, register_id=op.register_id)
    provider = gateway(row.profile)
    allowed = shop_fiscal.permitted(actor, permission(op.kind))
    enabled = await is_enabled(session, provider)
    safe_recovery = provider.available and (not provider.test_only or await shop_sales.test_database_is_safe(session))
    doc = await session.scalar(select(Document).where(Document.operation_id == op.id))
    truth = op.__dict__.get("acceptance")
    valid = truth_valid(op)
    return dto.OperationView(id=op.id, kind=op.kind, status=op.status, revision=op.revision,
        authority=op.authority, transport=op.transport, order_id=op.order_id, document_id=doc.id if doc else None,
        provider_uuid=op.provider_uuid, shift_id=op.shift_id, snapshot_hash=op.snapshot_hash,
        dispatched_at=op.dispatched_at, acceptance_verified=valid, accepted_at=truth.accepted_at if valid else None,
        result=truth.proof if valid else None,
        actions={"process": bool(allowed and enabled and op.status == "queued"),
            "cancel": bool(allowed and enabled and op.status == "queued" and op.dispatched_at is None),
            "reconcile": bool(allowed and safe_recovery and op.status in {"unknown", "pending"}),
            "replay_process": allowed, "replay_reconcile": allowed, "replay_cancel": allowed},
        blocking_reasons=[] if enabled else ["shop_checkbox_disabled"])


async def document_view(session, actor, doc):
    op = await load_operation(session, doc.operation_id)
    view = await operation_view(session, actor, op)
    proof = view.result
    return dto.DocumentView(id=doc.id, order_id=doc.order_id, return_id=doc.return_id, kind=doc.kind,
        status="issued" if op.status == "accepted" else op.status, revision=op.revision, provider_uuid=op.provider_uuid,
        shift_id=op.shift_id, authority=op.authority, transport=op.transport,
        is_sent_dps=proof.get("is_sent_dps") if proof else None, acceptance_verified=view.acceptance_verified,
        amount_minor=op.snapshot["amount_minor"], snapshot=op.snapshot, snapshot_hash=op.snapshot_hash,
        operation=view, created_at=op.created_at, issued_at=view.accepted_at,
        actions={**view.actions, "cancel": bool(view.actions["process"] and op.dispatched_at is None),
            "print": bool(view.acceptance_verified), "replay_cancel": shop_fiscal.permitted(actor, permission(op.kind))},
        blocking_reasons=view.blocking_reasons)


def preview_view(preview):
    return dto.PreviewView(**{k: getattr(preview, k) for k in ("id", "order_id", "return_id", "kind",
        "expected_revision", "register_revision", "snapshot", "snapshot_hash", "expires_at")},
        authority=preview.snapshot["authority"], transport=preview.snapshot["transport"])


async def physical_receipt_for(session,item):
    if item.carrier_receipt_id:
        from app.services.shop_np_returns import physical_for
        return await physical_for(session,item)
    return await session.scalar(select(PhysicalReceipt).where(PhysicalReceipt.return_id==item.id))


async def return_view(session, actor, item):
    row = await register(session, register_id=item.register_id)
    enabled = await is_enabled(session, gateway(row.profile))
    ready = enabled and identity_valid(row) and row.shift_status == "open" and row.shift_id == item.shift_id
    busy = await pending(session, row)
    ready = ready and not busy
    refund = await session.scalar(select(Refund).where(Refund.return_id == item.id))
    physical = await physical_receipt_for(session,item)
    from app.services.shop_payment_resolutions import cancellation_valid
    physical_not_required = await cancellation_valid(session,item)
    doc = await session.scalar(select(Document).join(Operation, Operation.id == Document.operation_id).where(
        Document.return_id == item.id, Operation.status.notin_(["cancelled", "rejected"])))
    fields = ("id", "amount_minor", "method", "external_reference", "evidence", "authority", "created_at")
    sales = shop_fiscal.permitted(actor, "can_record_sales")
    inventory = shop_fiscal.permitted(actor, "can_post_inventory")
    fiscal = shop_fiscal.permitted(actor, "can_fiscalize_returns")
    return dto.ReturnView(id=item.id, order_id=item.order_id, sale_document_id=item.sale_document_id,
        revision=item.revision, authority=row.authority, transport=row.transport, amount_minor=item.amount_minor,carrier_receipt_id=item.carrier_receipt_id,
        cancellation_case_id=item.cancellation_case_id,physical_not_required=physical_not_required,
        lines=item.lines, reason=item.reason, refund={k: getattr(refund, k) for k in fields} if refund else None,
        physical_receipt=({k: getattr(physical, k) for k in ("id", "lines", "evidence", "operator_id", "created_at")}
            | ({"carrier_receipt_id":item.carrier_receipt_id} if item.carrier_receipt_id else {})) if physical else None,
        fiscal_document=await document_view(session, actor, doc) if doc else None,
        actions={"refund": bool(ready and sales and not refund), "physical_receipt": bool(ready and inventory and not physical and not physical_not_required),
            "receipt_preview": bool(ready and fiscal and refund and (physical or physical_not_required) and not doc),
            "approve_receipt": bool(ready and fiscal and refund and (physical or physical_not_required) and not doc),
            "replay_refund": sales, "replay_physical_receipt": inventory, "replay_receipt_preview": fiscal,
            "replay_approve_receipt": fiscal}, blocking_reasons=(["shop_checkbox_disabled"] if not enabled else [])
            + (["checkbox_register_busy"] if busy else []) + (["checkbox_shift_closed"] if row.shift_status != "open" else []))


async def register_view(session, actor, row):
    provider = gateway(row.profile)
    enabled = await is_enabled(session, provider)
    manage = shop_fiscal.permitted(actor, "can_manage_fiscal_settings")
    state = await reconciliation(session, row) if manage else None
    op = await pending(session, row)
    free = not op
    reports = list((await session.scalars(select(Operation).where(Operation.register_id == row.id,
        Operation.kind.in_(["x", "z", "shift_close"]), Operation.status == "accepted").order_by(Operation.id))).all()) if manage else []
    return dto.RegisterView(id=row.id, revision=row.revision, authority=row.authority, transport=row.transport,
        identity_verified=identity_valid(row), shift_id=row.shift_id, shift_status=row.shift_status,
        pending_operation=await operation_view(session, actor, op) if op and manage else None,
        actions={"open": bool(enabled and manage and free and identity_valid(row) and row.shift_status == "closed"),
            "close": bool(enabled and manage and free and row.shift_status == "open" and not any(state.values())),
            "z": bool(enabled and manage and free and row.shift_status == "open" and not any(state.values())),
            "x": bool(enabled and manage and free and row.shift_status == "open"),
            **{f"replay_{k}": manage for k in ("open", "close", "x", "z")}}, reconciliation=state,
        reports=[await operation_view(session, actor, report) for report in reports])


async def readiness(session, actor):
    provider = gateway()
    row = await register(session, provider)
    enabled = await is_enabled(session, provider)
    reasons = list(getattr(provider, "blocking_reasons", []))
    if not enabled:
        reasons.append("shop_checkbox_disabled")
    if not identity_valid(row):
        reasons.append("checkbox_identity_required")
    return dto.ReadinessView(profile_id=(provider.profile or {}).get("id"), configured=provider.available, enabled=enabled,
        authority="test" if provider.test_only else "provider" if provider.available else "none",
        transport=provider.transport, identity_verified=identity_valid(row), policy_available=bool(enabled and identity_valid(row)),
        register=await register_view(session, actor, row) if row else None,
        actions={"verify": bool(enabled and shop_fiscal.permitted(actor, "can_manage_fiscal_settings")),
            "replay_verify": shop_fiscal.permitted(actor, "can_manage_fiscal_settings")}, blocking_reasons=list(dict.fromkeys(reasons)))


async def order_view(session, actor, order):
    provider = gateway()
    bound = await session.get(Binding, order.id)
    row = await register(session, register_id=bound.register_id) if bound else await register(session, provider)
    if row:
        provider = gateway(row.profile)
    enabled = await is_enabled(session, provider)
    reasons = [] if enabled else ["shop_checkbox_disabled"]
    if row:
        try:
            await binding(session, order, row)
        except HTTPException as error:
            reasons.append(error.detail["code"])
        if not identity_valid(row):
            reasons.append("checkbox_identity_required")
        if row.shift_status != "open":
            reasons.append("checkbox_shift_closed")
        if await pending(session, row):
            reasons.append("checkbox_register_busy")
    else:
        reasons.append("checkbox_identity_required")
    if not shop_sales.canonical_payment(order):
        reasons.append("payment_required")
    sale = await sale_for(session, order.id)
    returns = list((await session.scalars(select(FinancialReturn).where(FinancialReturn.order_id == order.id).order_by(FinancialReturn.id))).all())
    fiscal = shop_fiscal.permitted(actor, "can_fiscalize_sales")
    sales = shop_fiscal.permitted(actor, "can_record_sales")
    sale_reasons = list(reasons)
    if order.fulfillment_method != "pickup":
        reasons.append("carrier_return_pending_live")
    create_return = bool(enabled and sales and sale and truth_valid(sale.operation)
        and shop_sales.canonical_fulfillment(order) and order.fulfillment_method == "pickup")
    ready_sale = enabled and fiscal and not sale_reasons and not sale
    return dto.OrderFiscalView(enabled=bool(enabled and "checkbox_history_unbound" not in reasons and "checkbox_profile_conflict" not in reasons), order_id=order.id, revision=order.revision,
        authority=row.authority if row else "none", transport=provider.transport, sale_document=await document_view(session, actor, sale) if sale else None,
        returns=[await return_view(session, actor, item) for item in returns],
        actions={"sale_preview": bool(ready_sale), "approve_sale": bool(ready_sale), "create_return": create_return,
            "replay_sale_preview": fiscal, "replay_approve_sale": fiscal, "replay_create_return": sales}, blocking_reasons=reasons)


async def report_basis(session, row):
    operations = list((await session.scalars(select(Operation).where(Operation.register_id == row.id,
        Operation.shift_id == row.shift_id, Operation.kind.in_(["sale", "return"]), Operation.status == "accepted"))).all())
    totals = {"sales_minor": 0, "returns_minor": 0, "net_minor": 0, "sale_count": 0, "return_count": 0}
    groups, taxes = {}, {}
    for op in operations:
        if not truth_valid(op):
            fail("checkbox_proof_mismatch", "Accepted receipt has invalid durable proof")
        amount, payment = op.snapshot["amount_minor"], op.snapshot["payment"]
        sale = op.kind == "sale"
        totals["sales_minor" if sale else "returns_minor"] += amount
        totals["sale_count" if sale else "return_count"] += 1
        key = (payment["type"], payment["label"], payment["code"])
        group = groups.setdefault(key, dict(type=key[0], label=key[1], code=key[2], sell_sum=0, return_sum=0))
        group["sell_sum" if sale else "return_sum"] += amount
        for tax in op.acceptance.proof.get("taxes", []):
            aggregate = taxes.setdefault(tax["code"], {"code": tax["code"], **row.profile["taxes"][str(tax["code"])],
                "sell_sum": 0, "return_sum": 0, "sales_turnover": 0, "returns_turnover": 0})
            aggregate["sell_sum" if sale else "return_sum"] += tax["value"]
            aggregate["sales_turnover" if sale else "returns_turnover"] += tax["turnover_minor"]
    totals["net_minor"] = totals["sales_minor"] - totals["returns_minor"]
    return {"totals": totals, "payments": sorted(groups.values(), key=lambda g: (g["type"], g["label"], g["code"])),
        "taxes": sorted(taxes.values(), key=lambda t: t["code"])}


def new_operation(row, actor, kind, state, *, order_id=None, provider_uuid=None):
    state = {**state, "profile_id": row.id, "profile_hash": row.profile_hash,
        "organization_id": row.profile["organization_id"], "register_id": row.profile["register_id"],
        "cashier_id": row.profile["cashier_id"], "authority": row.authority, "transport": row.transport}
    return Operation(register_id=row.id, order_id=order_id, kind=kind, status="queued", revision=1,
        authority=row.authority, transport=row.transport, provider_uuid=provider_uuid,
        shift_id=state.get("shift_id"), snapshot=state, snapshot_hash=digest(state), operator_id=actor.id,
        created_at=datetime.now(UTC))


async def register_command(session, actor, payload, key, kind):
    try:
        actor = await actor_for(session, actor, "can_manage_fiscal_settings")
        provider = gateway()
        await guard(session, provider, recovery=True)
        if payload.expected_profile_id != provider.profile["id"]:
            fail("checkbox_profile_conflict", "The configured profile changed; reload the server-owned profile")
        row = await register(session, provider, lock=True, create=True)
        # Close and Z are the same semantic provider effect, with the same fence.
        kind = "z" if kind == "shift_close" else kind
        previous = await cached(session, actor, row, kind, key, payload, dto.OperationView)
        if previous:
            return previous
        await guard(session, provider)
        await free_register(session, row)
        if kind != "verify":
            revision(row, payload.expected_revision)
            if not identity_valid(row):
                fail("checkbox_identity_required", "Verify the configured identities first")
        if kind == "shift_open" and row.shift_status != "closed":
            fail("checkbox_shift_unknown", "The register already has an active or unknown shift")
        if kind in {"x", "z"}:
            open_register(row)
        if kind == "z" and any((await reconciliation(session, row)).values()):
            fail("shift_unsettled", "Resolve pending receipts, unreceipted money and returns before closing")
        state = {"shift_id": row.shift_id, "previous_shift_status": row.shift_status}
        if kind in {"x", "z"}:
            state.update(await report_basis(session, row))
        provider_uuid = str(uuid4()) if kind == "shift_open" else None
        if provider_uuid:
            state["shift_id"] = provider_uuid
        op = new_operation(row, actor, kind, state, provider_uuid=provider_uuid)
        command = await intent(session, actor, row, kind, key, payload)
        session.add(op)
        row.revision += 1
        await session.flush()
        return await finish(session, actor, command, await operation_view(session, actor, op), kind)
    except Exception:
        await session.rollback()
        raise


def proof_matches(op, row, proof):
    if (not isinstance(proof, dict) or proof.get("verified") is not True
            or proof.get("authority") != op.authority or proof.get("transport") != op.transport
            or any(proof.get(k) != row.profile[k] for k in ("organization_id", "register_id", "cashier_id"))):
        fail("checkbox_proof_mismatch", "Provider proof does not bind the immutable fiscal identities")
    if op.authority == "test" and proof.get("is_sent_dps") is True:
        fail("checkbox_proof_mismatch", "Mock transport cannot claim submission to DPS")


def parsed_time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return aware(value) if isinstance(value, datetime) else None


def validate_report(op, row, proof, *, z):
    proof_matches(op, row, proof)
    if (proof.get("resource") != "report" or proof.get("shift_id") != op.shift_id
            or proof.get("is_z_report") is not z or proof.get("totals") != op.snapshot["totals"]
            or sorted(proof.get("payments", []), key=lambda g: (g["type"], g["label"], g["code"])) != op.snapshot["payments"]
            or proof.get("taxes") != op.snapshot["taxes"]):
        fail("checkbox_proof_mismatch", "Report must match the exact shift, receipt totals and payment groups")
    created = parsed_time(proof.get("created_at"))
    if (not created or not op.dispatched_at or created < aware(op.dispatched_at) - timedelta(seconds=30)
            or created > datetime.now(UTC) + timedelta(seconds=30)):
        fail("checkbox_proof_mismatch", "Report predates this operation dispatch")
    if (not proof.get("id") or proof.get("accepted") is not True
            or z and (not proof.get("fiscal_code") or proof.get("is_sent_dps") is not (op.authority == "provider"))):
        fail("checkbox_proof_mismatch", "Report lacks verified terminal fiscal acceptance")


async def apply_proof(session, actor, row, op, proof):
    proof_matches(op, row, proof)
    if proof.get("rejected") is True and op.kind in {"sale", "return"}:
        if (proof.get("id") != op.provider_uuid or proof.get("snapshot_hash") != op.snapshot_hash
                or proof.get("kind") != op.kind or proof.get("amount_minor") != op.snapshot["amount_minor"]
                or proof.get("shift_id") != op.shift_id or proof.get("is_sent_dps") is not False
                or proof.get("resource") != "receipt" or proof.get("status") != "ERROR"
                or proof.get("accepted") is not False or proof.get("is_created_offline") is not False
                or proof.get("transaction_status") != "ERROR" or proof.get("transaction_offline") is not False
                or proof.get("transaction_response_accepted") is not False
                or proof.get("related_receipt_id") != op.snapshot["related_receipt_id"] or proof.get("currency") != "UAH"
                or proof.get("fiscal_code")):
            fail("checkbox_proof_mismatch", "A rejected receipt must still bind the exact original dispatch")
        evidence = audit(session, actor, "provider_rejection", operation_id=op.id,
            provider_uuid=op.provider_uuid, snapshot_hash=op.snapshot_hash, proof=proof, proof_hash=digest(proof),
            proof_canonical=canonical_json(proof))
        await session.flush([evidence])
        op.status, op.revision = "rejected", op.revision + 1
        row.revision += 1
        return
    if proof.get("accepted") is not True:
        op.status, op.revision = "pending", op.revision + 1
        row.revision += 1
        return
    if op.kind in {"sale", "return"}:
        if (proof.get("resource") != "receipt" or proof.get("id") != op.provider_uuid
                or proof.get("snapshot_hash") != op.snapshot_hash or proof.get("kind") != op.kind
                or proof.get("shift_id") != op.shift_id or proof.get("amount_minor") != op.snapshot["amount_minor"]
                or proof.get("currency") != "UAH" or proof.get("related_receipt_id") != op.snapshot["related_receipt_id"]
                or not proof.get("fiscal_code") or proof.get("is_sent_dps") is not (op.authority == "provider")):
            fail("checkbox_proof_mismatch", "Receipt does not match its exact approved payment, goods and fiscal proof")
        provider_id = "receipt:" + op.provider_uuid
    elif op.kind == "verify":
        if proof.get("resource") != "identity" or proof.get("is_test") is not (op.authority == "test"):
            fail("checkbox_identity_mismatch", "Fiscal identities have the wrong environment")
        row.identity, row.verified_at = proof, datetime.now(UTC)
        provider_id = "verification:" + str(op.id)
    elif op.kind == "shift_open":
        if proof.get("resource") != "shift" or proof.get("id") != op.provider_uuid or proof.get("status") != "OPENED":
            fail("checkbox_proof_mismatch", "Open shift proof must match the original reserved UUID")
        row.shift_id, row.shift_status = op.provider_uuid, "open"
        provider_id = "open:" + op.provider_uuid
    elif op.kind == "x":
        validate_report(op, row, proof, z=False)
        provider_id = "report:" + proof["id"]
    else:
        if proof.get("resource") != "shift" or proof.get("id") != op.shift_id or proof.get("status") != "CLOSED":
            fail("checkbox_proof_mismatch", "Close must prove the exact original shift")
        report = proof.get("z_report")
        validate_report(op, row, report, z=True)
        row.shift_status = "closed"
        provider_id = "report:" + report["id"]
    existing = await session.get(Acceptance, op.id)
    if not existing:
        session.add(Acceptance(operation_id=op.id, provider_id=provider_id, snapshot_hash=op.snapshot_hash,
            proof=proof, proof_hash=digest(proof), proof_canonical=canonical_json(proof), accepted_at=datetime.now(UTC)))
        await session.flush()
    op.status, op.revision = "accepted", op.revision + 1
    row.revision += 1
    audit(session, actor, "provider_acceptance", operation_id=op.id, provider_id=provider_id)
    await session.flush()
    await session.refresh(op, ["acceptance"])


async def provider_effect(provider, op, *, dispatch, candidate=None):
    if op.kind in {"sale", "return"}:
        return await provider.create_receipt(op.provider_uuid, op.snapshot) if dispatch else await provider.get_receipt(op.provider_uuid, op.snapshot)
    if op.kind == "verify":
        return await provider.verify_identity()
    if op.kind == "shift_open":
        return await provider.open_shift(op.provider_uuid) if dispatch else await provider.get_shift(op.provider_uuid)
    if op.kind == "x":
        if dispatch:
            return await provider.create_x_report()
        if not candidate:
            fail("checkbox_report_candidate_required", "Supply a discovered report UUID for verified lookup; X cannot be repeated")
        return await provider.get_report(str(candidate))
    return await provider.close_shift(op.shift_id) if dispatch else await provider.get_shift(op.shift_id)


async def operation_command(session, actor, identifier, payload, key, operation, *, document_id=None):
    try:
        op = await load_operation(session, identifier)
        if document_id is not None:
            doc = await load_document(session, document_id)
            if doc.operation_id != op.id:
                raise HTTPException(404, "Checkbox document operation not found")
        actor = await actor_for(session, actor, permission(op.kind))
        actor_id = actor.id
        row = await register(session, register_id=op.register_id)
        scope = f"operation:{op.id}:{operation}"
        previous = await cached(session, actor, row, scope, key, payload, dto.OperationView)
        if previous:
            return previous
        provider = gateway(row.profile)
        await guard(session, provider, recovery=op.status in {"unknown", "pending", "accepted"})
        if operation == "process" and op.status == "queued" and op.dispatched_at is None:
            await actor_for(session, SimpleNamespace(id=op.operator_id), permission(op.kind))
        if op.order_id:
            await shop_sales.load_order(session, op.order_id, lock=True)
        row = await register(session, provider, lock=True)
        op = await load_operation(session, identifier, lock=True)
        command = await intent(session, actor, row, scope, key, payload)
        if command.response_json:
            return dto.OperationView.model_validate(command.response_json)
        revision(op, payload.expected_revision)
        if op.status == "accepted":
            return await finish(session, actor, command, await operation_view(session, actor, op), operation, op.order_id)
        dispatch = operation == "process" and op.status == "queued" and op.dispatched_at is None
        if op.order_id:
            await binding(session, await shop_sales.load_order(session, op.order_id), row)
        if dispatch:
            if op.kind=="return" and op.order_id:
                from app.services.shop_payment_resolutions import financial_review_guard
                await financial_review_guard(session,op.order_id)
            await guard(session, provider)
            await free_register(session, row, except_id=op.id)
            if op.kind not in {"verify", "shift_open"}:
                open_register(row)
                if row.shift_id != op.shift_id:
                    fail("checkbox_shift_unknown", "Restore the operation's exact original shift")
            if op.kind in {"z", "shift_close"}:
                state = await reconciliation(session, row)
                state["pending_operation_ids"] = [i for i in state["pending_operation_ids"] if i != op.id]
                if any(state.values()) or await report_basis(session, row) != {k: op.snapshot[k] for k in ("totals", "payments", "taxes")}:
                    fail("shift_unsettled", "The approved close basis changed")
            op.dispatched_at, op.status, op.revision = datetime.now(UTC), "unknown", op.revision + 1
            if op.kind in {"shift_open", "z", "shift_close"}:
                row.shift_status = "unknown"
            row.revision += 1
            acknowledgment = await operation_view(session, actor, op)
            await finish(session, actor, command, acknowledgment, "dispatch_fence", op.order_id)
        elif op.status not in {"unknown", "pending"}:
            fail("checkbox_operation_unknown", "Only queued work or the original uncertain operation is supported")
        else:
            acknowledgment = None
            await session.commit()  # release business locks before read-only provider recovery
        effect = SimpleNamespace(kind=op.kind, provider_uuid=op.provider_uuid, shift_id=op.shift_id,
            snapshot=op.snapshot)
        op_kind, order_id = op.kind, op.order_id
        await safe(session, provider)
        await session.rollback()
        try:
            proof = await provider_effect(provider, effect, dispatch=dispatch,
                candidate=getattr(payload, "candidate_report_uuid", None))
        except Exception as error:
            if dispatch:
                from app.services.checkbox_provider import CheckboxProviderError
                if isinstance(error, CheckboxProviderError) and error.dispatched is False:
                    # This records technical evidence of an already authorized
                    # attempt, not permission for a new financial action. A role
                    # revocation must not strand a provably undispatched fence.
                    actor = SimpleNamespace(id=actor_id)
                    if order_id:
                        await shop_sales.load_order(session, order_id, lock=True)
                    row = await register(session, provider, lock=True)
                    op = await load_operation(session, identifier, lock=True)
                    if op.status == "unknown":
                        evidence = audit(session, actor, "provider_no_dispatch", operation_id=op.id, code=error.code,
                            business_request_dispatched=False)
                        await session.flush([evidence])
                        op.status, op.revision = "rejected", op.revision + 1
                        if op.kind in {"shift_open", "z", "shift_close"}:
                            row.shift_status = op.snapshot["previous_shift_status"]
                        row.revision += 1
                        await session.commit()
                return acknowledgment  # unknown fence and exact accepted response remain committed
            if hasattr(error, "code"):
                fail(error.code, "Provider lookup could not verify the original operation")
            raise
        try:
            actor = await actor_for(session, SimpleNamespace(id=actor_id), permission(op_kind))
            if order_id:
                await shop_sales.load_order(session, order_id, lock=True)
            row = await register(session, provider, lock=True)
            op = await load_operation(session, identifier, lock=True)
            if proof is not None and op.status != "accepted":
                await apply_proof(session, actor, row, op, proof)
            command = await intent(session, actor, row, scope, key, payload)
            result = acknowledgment or await operation_view(session, actor, op)
            return await finish(session, actor, command, result, "reconcile", op.order_id)
        except Exception:
            await session.rollback()
            if dispatch:
                return acknowledgment
            raise
    except IntegrityError:
        await session.rollback()
        fail("checkbox_proof_mismatch", "Provider proof or operation already belongs to another resource")
    except Exception:
        await session.rollback()
        raise


async def cancel_operation(session, actor, identifier, payload, key, *, document=False):
    try:
        doc = await load_document(session, identifier) if document else None
        op = await load_operation(session, doc.operation_id if doc else identifier)
        actor = await actor_for(session, actor, permission(op.kind))
        row = await register(session, register_id=op.register_id)
        scope = f"operation:{op.id}:cancel"
        view = dto.DocumentView if doc else dto.OperationView
        previous = await cached(session, actor, row, scope, key, payload, view)
        if previous:
            return previous
        provider = gateway(row.profile)
        await guard(session, provider)
        if op.order_id:
            await shop_sales.load_order(session, op.order_id, lock=True)
        row = await register(session, provider, lock=True)
        op = await load_operation(session, op.id, lock=True)
        revision(op, payload.expected_revision)
        if op.status != "queued" or op.dispatched_at is not None:
            fail("checkbox_operation_unknown", "A provider-dispatched operation cannot be cancelled")
        command = await intent(session, actor, row, scope, key, payload)
        op.status, op.revision = "cancelled", op.revision + 1
        row.revision += 1
        await session.flush()
        result = await document_view(session, actor, doc) if doc else await operation_view(session, actor, op)
        return await finish(session, actor, command, result, "cancel", op.order_id)
    except Exception:
        await session.rollback()
        raise


async def order_command(session, actor, order_id, payload, key, operation, *, return_id=None):
    try:
        field = "can_record_sales" if operation == "create_return" else "can_fiscalize_returns" if return_id else "can_fiscalize_sales"
        actor = await actor_for(session, actor, field)
        order = await shop_sales.load_order(session, order_id, lock=True)
        bound = await session.get(Binding, order.id)
        row = await register(session, register_id=bound.register_id) if bound else None
        provider = gateway(row.profile if row else None)
        if not row:
            await guard(session, provider)
            row = await register(session, provider, create=True)
        scope = f"order:{order.id}:{operation}:return:{return_id}"
        view = dto.ReturnView if operation == "create_return" else dto.DocumentView if operation == "approve" else dto.PreviewView
        previous = await cached(session, actor, row, scope, key, payload, view)
        if previous:
            return previous
        await guard(session, provider)
        canonical_order(order)
        if return_id or operation=="create_return":
            from app.services.shop_payment_resolutions import financial_review_guard
            await financial_review_guard(session,order.id)
        row = await register(session, provider, lock=True)
        await free_register(session, row)
        open_register(row)
        await binding(session, order, row, create=True)
        item = await session.get(FinancialReturn, return_id, populate_existing=True) if return_id else None
        if return_id and (not item or item.order_id != order_id or item.register_id != row.id):
            raise HTTPException(404, "Checkbox financial return not found")
        revision(item or order, payload.expected_revision)
        command = await intent(session, actor, row, scope, key, payload)
        if command.response_json:
            return view.model_validate(command.response_json)
        if operation == "create_return":
            if order.fulfillment_method != "pickup":
                fail("carrier_return_pending_live", "Production NP physical returns require a separate real carrier integration")
            if not shop_sales.canonical_fulfillment(order):
                fail("pre_handover_return_blocked", "Paid cancellation before handover requires its dedicated workflow")
            original = await load_document(session, payload.sale_document_id)
            original_op = await issued(session, original)
            confirmation = shop_sales.canonical_payment(order)
            if original.order_id != order.id or original.kind != "sale" or original.payment_confirmation_id != confirmation.id:
                fail("sale_receipt_required", "The original sale must bind this exact order and payment")
            used = {}
            for previous_return in (await session.scalars(select(FinancialReturn).where(FinancialReturn.order_id == order.id))).all():
                for line in previous_return.lines:
                    used[line["order_item_id"]] = used.get(line["order_item_id"], 0) + line["quantity"]
            lines = shop_fiscal.allocate_return_lines(original_op.snapshot,
                [line.model_dump() for line in payload.lines], used)
            amount = sum(line["total_minor"] for line in lines)
            if amount <= 0:
                fail("unsupported_zero_total", "A financial return must have a positive exact amount")
            if original_op.snapshot["payment"]["type"] == "CASH" and amount % 100:
                fail("checkbox_cash_rounding_unsupported", "This partial cash return requires an unsupported rounding policy")
            item = FinancialReturn(order_id=order.id, sale_document_id=original.id, register_id=row.id,
                shift_id=row.shift_id, revision=1, amount_minor=amount, lines=lines,
                reason=payload.reason, operator_id=actor.id, created_at=datetime.now(UTC))
            session.add(item)
            order.revision += 1
            await session.flush()
            return await finish(session, actor, command, await return_view(session, actor, item), operation, order.id)
        existing = await session.scalar(select(Document).join(Operation, Operation.id == Document.operation_id).where(
            Document.order_id == order.id, Document.return_id == return_id,
            Operation.status.notin_(["cancelled", "rejected"])))
        if existing:
            fail("document_exists", "Recover the existing canonical Checkbox receipt")
        state = await snapshot(session, order, row, item)
        if operation == "preview":
            preview = Preview(order_id=order.id, return_id=return_id, register_id=row.id, actor_id=actor.id,
                kind=state["kind"], expected_revision=payload.expected_revision, register_revision=row.revision,
                snapshot=state, snapshot_hash=digest(state), expires_at=datetime.now(UTC) + timedelta(minutes=10))
            session.add(preview)
            await session.flush()
            return await finish(session, actor, command, preview_view(preview), operation, order.id)
        preview = await session.get(Preview, payload.preview_id)
        if (not preview or preview.order_id != order.id or preview.return_id != return_id or preview.actor_id != actor.id
                or preview.register_id != row.id or preview.expected_revision != payload.expected_revision
                or preview.register_revision != row.revision or aware(preview.expires_at) < datetime.now(UTC)
                or preview.snapshot_hash != digest(state) or digest(preview.snapshot) != preview.snapshot_hash):
            fail("preview_stale", "The immutable approval preview does not match this exact current resource")
        op = new_operation(row, actor, state["kind"], state, order_id=order.id, provider_uuid=str(uuid4()))
        session.add(op)
        row.revision += 1
        await session.flush()
        doc = Document(order_id=order.id, payment_confirmation_id=shop_sales.canonical_payment(order).id,
            return_id=return_id, preview_id=preview.id, operation_id=op.id, kind=state["kind"])
        session.add(doc)
        if item:
            item.revision += 1
        else:
            order.revision += 1
        await session.flush()
        return await finish(session, actor, command, await document_view(session, actor, doc), operation, order.id)
    except IntegrityError:
        await session.rollback()
        fail("document_exists", "This preview, receipt or return already has canonical work")
    except Exception:
        await session.rollback()
        raise


async def return_command(session, actor, identifier, payload, key, operation):
    try:
        actor = await actor_for(session, actor, "can_record_sales" if operation == "refund" else "can_post_inventory")
        item = await session.get(FinancialReturn, identifier)
        if not item:
            raise HTTPException(404, "Checkbox return not found")
        order = await shop_sales.load_order(session, item.order_id, lock=True)
        row = await register(session, register_id=item.register_id)
        scope = f"return:{item.id}:{operation}"
        previous = await cached(session, actor, row, scope, key, payload, dto.ReturnView)
        if previous:
            return previous
        provider = gateway(row.profile)
        await guard(session, provider)
        row = await register(session, provider, lock=True)
        await free_register(session, row)
        open_register(row)
        await binding(session, order, row)
        await session.refresh(item)
        revision(item, payload.expected_revision)
        if row.shift_id != item.shift_id or (order.fulfillment_method != "pickup" and not item.carrier_receipt_id and not item.cancellation_case_id):
            fail("checkbox_shift_unknown", "Resolve this pickup return in its assigned open shift")
        if operation != "refund" and (item.carrier_receipt_id or item.cancellation_case_id):
            fail("physical_receipt_exists", "The canonical Nova Poshta parcel receipt is already linked; stock must not move twice")
        command = await intent(session, actor, row, scope, key, payload)
        if command.response_json:
            return dto.ReturnView.model_validate(command.response_json)
        from app.services.shop_payment_resolutions import financial_review_guard
        await financial_review_guard(session,order.id)
        original = await issued(session, await load_document(session, item.sale_document_id))
        if operation == "refund":
            if await session.scalar(select(Refund.id).where(Refund.return_id == item.id)):
                fail("refund_exists", "The explicit staff refund is already recorded")
            session.add(Refund(return_id=item.id, amount_minor=item.amount_minor,
                method=original.snapshot["payment"]["type"], authority="test" if row.authority == "test" else "manual",
                external_reference=payload.external_reference, evidence=payload.evidence,
                operator_id=actor.id, created_at=datetime.now(UTC)))
        else:
            if await session.scalar(select(PhysicalReceipt.id).where(PhysicalReceipt.return_id == item.id)):
                fail("physical_receipt_exists", "Physical receipt is already recorded")
            requested = {line.order_item_id: line for line in payload.lines}
            if len(requested) != len(payload.lines) or {k: v.quantity for k, v in requested.items()} != {
                    line["order_item_id"]: line["quantity"] for line in item.lines}:
                fail("receipt_quantity_mismatch", "Physical receipt must cover the exact claimed quantities")
            fulfillment = shop_sales.canonical_fulfillment(order)
            if not fulfillment or fulfillment.kind != "pickup":
                fail("physical_receipt_required", "Original canonical pickup handover is required")
            products = {p.id: p for p in (await session.scalars(select(Product).where(Product.id.in_(
                sorted({line["product_id"] for line in item.lines}))).order_by(Product.id).with_for_update())).all()}
            for line in item.lines:
                shipped = await session.scalar(select(func.coalesce(func.sum(-InventoryMovement.on_hand_delta), 0)).where(
                    InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == line["order_item_id"],
                    InventoryMovement.product_id == line["product_id"], InventoryMovement.movement_type == InventoryMovementType.shipment,
                    InventoryMovement.reference_key == f'order:{order.id}:item:{line["order_item_id"]}:shipment'))
                restored = await session.scalar(select(func.coalesce(func.sum(InventoryMovement.on_hand_delta), 0)).where(
                    InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == line["order_item_id"],
                    InventoryMovement.product_id == line["product_id"], InventoryMovement.movement_type == InventoryMovementType.customer_return))
                if restored + line["quantity"] > shipped or line["product_id"] not in products:
                    fail("shipped_quantity_exceeded", "Physical receipt requires the canonical shipped ledger")
                if requested[line["order_item_id"]].disposition == "restock":
                    products[line["product_id"]].stock_quantity += line["quantity"]
                    session.add(InventoryMovement(product_id=line["product_id"], movement_type=InventoryMovementType.customer_return,
                        quantity=line["quantity"], on_hand_delta=line["quantity"], reserved_delta=0,
                        order_id=order.id, order_item_id=line["order_item_id"], performed_by_user_id=actor.id,
                        reference_key=f'shop-checkbox-return:{item.id}:{line["order_item_id"]}', reason=payload.evidence))
            session.add(PhysicalReceipt(return_id=item.id, lines=[v.model_dump() for v in sorted(payload.lines, key=lambda v: v.order_item_id)],
                evidence=payload.evidence, operator_id=actor.id, created_at=datetime.now(UTC)))
        item.revision += 1
        await session.flush()
        return await finish(session, actor, command, await return_view(session, actor, item), operation, order.id)
    except IntegrityError:
        await session.rollback()
        fail("external_reference_used", "Refund reference or physical receipt already belongs to an accepted fact")
    except Exception:
        await session.rollback()
        raise


async def read(session, actor, resource, identifier=None):
    actor = await actor_for(session, actor)
    if resource in {"readiness", "register"}:
        state = await readiness(session, actor)
        if resource == "register" and not state.register:
            fail("checkbox_profile_unconfigured", "No register history exists for this configured profile")
        return state if resource == "readiness" else state.register
    if resource == "order":
        return await order_view(session, actor, await shop_sales.load_order(session, identifier))
    if resource == "profile":
        order = await shop_sales.load_order(session, identifier)
        return dto.ProfileView(order_id=order.id, fiscal_profile=profile_metadata(order))
    if resource == "document":
        doc = await load_document(session, identifier)
        return await document_view(session, actor, doc)
    if resource in {"operation", "report"}:
        op = await load_operation(session, identifier)
        if resource == "report" or not op.order_id:
            if not shop_fiscal.permitted(actor, "can_manage_fiscal_settings"):
                raise HTTPException(403, "Fiscal settings permission required")
        return await operation_view(session, actor, op)
    model = Preview if resource == "preview" else FinancialReturn
    item = await session.get(model, identifier)
    if not item:
        raise HTTPException(404, "Checkbox resource not found")
    if resource == "preview":
        if item.actor_id != actor.id or not shop_fiscal.permitted(actor, permission(item.kind)):
            raise HTTPException(403, "Current preview owner permission required")
        return preview_view(item)
    return await return_view(session, actor, item)


async def print_document(session, actor, identifier):
    actor = await actor_for(session, actor)
    doc = await load_document(session, identifier)
    op = await issued(session, doc)
    label = "TEST MOCK HTTP — не фіскальний чек" if op.authority == "test" else "Checkbox — локальна копія перевіреного чека"
    items = "".join(f'<li>{escape(line["name"])} × {line["quantity"]}: {line["total_minor"] / 100:.2f} UAH</li>' for line in op.snapshot["items"])
    return f'<!doctype html><html lang="uk"><meta charset="utf-8"><title>Shop Checkbox</title><h1>{label}</h1><p>{escape(op.provider_uuid)}</p><ul>{items}</ul><p>{op.snapshot["amount_minor"] / 100:.2f} UAH</p></html>'


class CheckboxFiscalPolicy:
    def __init__(self, order=None):
        bound = order.__dict__.get("shop_checkbox_binding") if order else None
        stored = bound.__dict__.get("register") if bound else None
        self.provider = gateway(stored.profile if stored else None)
        self.test_only = self.provider.test_only
        self.available = self.provider.available and settings.shop_checkbox_enabled

    def payment_methods(self, order):
        if not settings.shop_checkbox_enabled:
            return [], ["shop_checkbox_disabled"]
        if not self.provider.available:
            return [], ["checkbox_profile_unconfigured"]
        methods, reasons = [], []
        for method in (["cod"] if shop_sales.is_cod_order(order) else ["cash", "card", "bank_transfer"]):
            try:
                preflight_payment(order, self.provider.profile, method)
                methods.append(method)
            except HTTPException as error:
                if isinstance(error.detail, dict):
                    reasons.append(error.detail["code"])
        return methods, [] if methods else list(dict.fromkeys(reasons))

    async def money_readiness(self, session, order):
        bound = await session.get(Binding, order.id)
        row = await register(session, register_id=bound.register_id) if bound else await register(session, self.provider)
        if row:
            try:
                await binding(session, order, row)
            except HTTPException as error:
                return [error.detail["code"]]
        if not identity_valid(row):
            return ["checkbox_identity_required"]
        if row.shift_status != "open":
            return ["checkbox_shift_closed"]
        return ["checkbox_register_busy"] if await pending(session, row) else []

    async def readiness(self, session, order, phase):
        bound = await session.get(Binding, order.id)
        row = await register(session, register_id=bound.register_id) if bound else await register(session, self.provider)
        if row:
            self.provider = gateway(row.profile)
        if not await is_enabled(session, self.provider):
            return ["shop_checkbox_disabled"]
        try:
            if not row:
                fail("checkbox_identity_required", "Verify a register first")
            await binding(session, order, row)
            await free_register(session, row)
            open_register(row)
            if await session.scalar(select(FinancialReturn.id).where(FinancialReturn.order_id == order.id).limit(1)):
                return ["active_return"]
            confirmation = shop_sales.canonical_payment(order)
            if not confirmation and phase == "dispatch" and shop_sales.is_cod_order(order):
                preflight_payment(order, row.profile, "cod")
                return [] if row.profile["phase_policy"]["allow_cod_dispatch_without_receipt"] else ["sale_receipt_required"]
            doc = await sale_for(session, order.id)
            op = await issued(session, doc)
            original_row = SimpleNamespace(id=row.id, profile=row.profile, profile_hash=row.profile_hash,
                authority=row.authority, transport=row.transport, shift_id=op.shift_id)
            if not confirmation or doc.payment_confirmation_id != confirmation.id or op.snapshot != await snapshot(session, order, original_row):
                return ["sale_receipt_required"]
            return []
        except HTTPException as error:
            return [error.detail.get("code", "sale_receipt_required") if isinstance(error.detail, dict) else "sale_receipt_required"]

    async def authorize_in_session(self, session, **expected):
        order = await shop_sales.load_order(session, expected["order_id"])
        bound = await session.get(Binding, order.id)
        row = await register(session, register_id=bound.register_id) if bound else None
        provider = gateway(row.profile if row else None)
        await guard(session, provider)
        row = await register(session, provider, lock=True, create=True)
        await binding(session, order, row, create=True)
        reasons = await self.readiness(session, order, expected["phase"])
        if reasons:
            fail(reasons[0], "The bound Checkbox policy has not authorized this operation")
        confirmation = shop_sales.canonical_payment(order)
        if expected["payment_id"] != (confirmation.payment_id if confirmation else None):
            fail("checkbox_proof_mismatch", "Fiscal policy does not match the exact payment")
        doc = await sale_for(session, order.id)
        verification = f"checkbox:{row.id}:cod-deferral:{row.shift_id}"
        if confirmation:
            op = await issued(session, doc)
            verification = f"checkbox:{op.provider_uuid}:{op.snapshot_hash}"
        return shop_sales.FiscalAuthorization(**expected, verification_id=verification,
            authority="test" if row.authority == "test" else "production")


def profile_metadata(order=None):
    if order and (order.__dict__.get("shop_checkbox_binding") or order.__dict__.get("shop_checkbox_documents")):
        return "checkbox"
    if order and (order.__dict__.get("shop_fiscal_documents") or order.__dict__.get("shop_carrier_returns")):
        return "local_mock"
    if settings.shop_checkbox_profile is not None or settings.shop_checkbox_enabled:
        return "checkbox"
    return "local_mock" if settings.shop_fiscal_mock_enabled else "unavailable"


def fiscal_profile(order=None):
    return "checkbox" if profile_metadata(order) == "checkbox" else None


def projection(order):
    documents = order.__dict__.get("shop_checkbox_documents", [])
    if not documents:
        return None
    sale = next((d for d in reversed(documents) if d.kind == "sale"), None)
    if not sale:
        return None
    op = sale.__dict__.get("operation")
    if not op:
        return "checkbox_fiscal_invalid"
    prefix = "simulated_checkbox_fiscal_" if op.authority == "test" else "checkbox_fiscal_"
    confirmation = shop_sales.canonical_payment(order)
    if (not confirmation or confirmation.id != sale.payment_confirmation_id or op.order_id != order.id
            or op.snapshot.get("payment_confirmation_id") != confirmation.id
            or digest(op.snapshot) != op.snapshot_hash):
        return prefix + "invalid"
    if op.status != "accepted":
        return prefix + op.status
    if not truth_valid(op):
        return prefix + "invalid"
    returns = order.__dict__.get("shop_checkbox_returns", [])
    if returns:
        accepted = sum(d.operation.snapshot["amount_minor"] for d in documents if d.kind == "return" and truth_valid(d.operation))
        if accepted < sum(r.amount_minor for r in returns):
            return prefix + "return_pending"
        return prefix + ("returned" if accepted == op.snapshot["amount_minor"] else "partial_return")
    return prefix + "issued"
