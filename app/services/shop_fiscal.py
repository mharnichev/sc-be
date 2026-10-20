"""Bounded Shop fiscal simulator. No transport, credentials, scheduler or DPS.

The provider acceptance transaction is independent of its caller. A committed
dispatch fence is never retried as a create; recovery only looks up that UUID.
"""
from datetime import UTC, datetime, timedelta
from html import escape
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import OrderIntent, OrderPayment
from app.models.product import Product
from app.models.shop_sales import ShopPaymentConfirmation
from app.models.shop_fiscal import (ShopFiscalRegister, ShopFiscalPreview, ShopFiscalDocument,
    ShopFiscalAcceptance, ShopFinancialReturn, ShopRefundFact, ShopReturnReceipt, ShopFiscalReport, ShopFiscalAudit)
from app.schemas.shop_fiscal import (PreviewView, DocumentView, ReturnView, RegisterView,
    OrderFiscalView, ReportView, NOTICE)
from app.services import shop_sales, sales_access
from app.services.order_documents import claim_intent, digest, operation_key
from app.services.fiscal import exact_minor
from app.services.fiscal_returns import net_lines

REGISTER = "SHOP-MOCK-KASA-1"
fail = shop_sales.fail
aware = shop_sales.aware


def permitted(actor, permission=None):
    fields = (permission,) if permission else ("can_record_sales", "can_post_inventory",
        "can_fiscalize_sales", "can_fiscalize_returns", "can_manage_fiscal_settings")
    return bool(actor and actor.is_active and (actor.is_superuser or any(getattr(actor, p, False) for p in fields)))


async def actor_for(session, actor, permission=None):
    current = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if current:
        await sales_access.deny_master(session, current)
    if not permitted(current, permission):
        raise HTTPException(403, "Shop fiscal permission required")
    return current


async def enabled(session):
    return settings.shop_fiscal_mock_enabled and await shop_sales.test_database_is_safe(session)


async def guard(session):
    if not settings.shop_fiscal_mock_enabled:
        fail("shop_fiscal_disabled", "Local Shop fiscal operations are disabled", 503)
    await shop_sales.require_test_database(session, "shop_fiscal_test_unsafe")


async def register(session, *, lock=False, create=False):
    if create:
        await session.execute(insert(ShopFiscalRegister).values(id=REGISTER, shift_status="closed", shift_revision=1)
            .on_conflict_do_nothing(index_elements=["id"]))
    query = select(ShopFiscalRegister).where(ShopFiscalRegister.id == REGISTER).execution_options(populate_existing=True)
    return await session.scalar(query.with_for_update() if lock else query)


async def payment_barrier(session):
    """Serialize accepted money with close even when the mock flag was revoked.

    Never creates a mock register in production. Existing local test registers
    retain their serialization role independently of new-work availability.
    """
    if not await shop_sales.test_database_is_safe(session):
        return
    row = await register(session)
    if row or settings.shop_fiscal_mock_enabled:
        await register(session, lock=True, create=True)


def open_shift(row):
    if not row or row.shift_status != "open":
        fail("shift_closed", "Open the dedicated Shop mock shift")


def revision(row, expected):
    if row.revision != expected:
        fail("revision_conflict", "Reload the current resource")


def audit(session, actor, operation, **facts):
    session.add(ShopFiscalAudit(operator_id=actor.id, operation=operation, facts=facts, created_at=datetime.now(UTC)))


async def intent(session, actor, key, scope, payload):
    return await claim_intent(session, operation_key(key, f"shop-fiscal:{actor.id}:{scope}"), digest(command_data(payload)))


def command_data(payload):
    data = payload.model_dump(mode="json")
    # Preserve B2 accepted request hashes when this new optional NP link is absent.
    if data.get("carrier_receipt_id") is None:
        data.pop("carrier_receipt_id", None)
    return data


async def cached(session, actor, key, scope, payload, view):
    row = await session.get(OrderIntent, operation_key(key, f"shop-fiscal:{actor.id}:{scope}"))
    if row and row.payload_hash != digest(command_data(payload)):
        fail("idempotency_conflict", "Key already used for another request")
    return view.model_validate(row.response_json) if row and row.response_json else None


async def finish(session, actor, command, result, operation, order_id=None):
    command.order_id, command.response_json = order_id, result.model_dump(mode="json")
    audit(session, actor, operation, order_id=order_id, result_id=getattr(result, "id", None))
    await session.commit()
    return result


def valid_order(order):
    if order.__dict__.get("shop_checkbox_binding") or order.__dict__.get("shop_checkbox_documents"):
        fail("checkbox_profile_conflict", "A Checkbox-bound order cannot use local fiscal B2/B3 facts")
    if not shop_sales.eligible_order(order):
        fail("shop_order_required", "A canonical non-simulated Shop order is required")
    confirmation = shop_sales.canonical_payment(order)
    if ((order.payments and not confirmation) or (order.handovers and not shop_sales.canonical_fulfillment(order))
            or order.qr_payment_attempts):
        fail("incompatible_sales_facts", "Legacy or simulated facts cannot become canonical Shop fiscal facts")
    return confirmation


def valid_document_truth(document):
    truth = document.__dict__.get("acceptance")
    return bool(document.status == "issued" and truth and truth.document_id == document.id
        and truth.provider_uuid == document.provider_uuid and truth.snapshot_hash == document.snapshot_hash
        and digest(document.snapshot) == document.snapshot_hash
        and document.snapshot.get("document_kind") == "shop_mock_fiscal_receipt"
        and document.snapshot.get("authority") == "test" and document.snapshot.get("is_sent_dps") is False)


def fiscal_projection(order):
    documents = order.__dict__.get("shop_fiscal_documents", [])
    if not documents or not shop_sales.canonical_payment(order):
        return None
    sales = [d for d in documents if d.kind == "sale"]
    if not sales:
        return None
    sale = sales[-1]
    confirmation = shop_sales.canonical_payment(order)
    if (sale.payment_confirmation_id != confirmation.id or sale.snapshot.get("payment_confirmation_id") != confirmation.id
            or digest(sale.snapshot) != sale.snapshot_hash):
        return "simulated_fiscal_invalid"
    if sale.status != "issued":
        return "simulated_fiscal_" + sale.status
    if not valid_document_truth(sale):
        return "simulated_fiscal_invalid"
    returns = order.__dict__.get("shop_financial_returns", [])
    if returns:
        issued_total = sum(d.snapshot["amount_minor"] for d in documents if d.kind == "return" and valid_document_truth(d))
        if issued_total < sum(r.amount_minor for r in returns):
            return "simulated_fiscal_return_pending"
        return "simulated_fiscal_returned" if issued_total == sale.snapshot["amount_minor"] else "simulated_fiscal_partial_return"
    return "simulated_fiscal_issued"


async def issued(session, document):
    if (not document or document.status != "issued" or digest(document.snapshot) != document.snapshot_hash
            or document.snapshot.get("document_kind") != "shop_mock_fiscal_receipt"
            or document.snapshot.get("authority") != "test" or document.snapshot.get("is_sent_dps") is not False):
        fail("sale_receipt_required", "An issued Shop test receipt with durable acceptance is required")
    truth = await session.get(ShopFiscalAcceptance, document.provider_uuid)
    if not truth or truth.document_id != document.id or truth.snapshot_hash != document.snapshot_hash:
        fail("sale_receipt_required", "Receipt lacks matching durable Shop provider acceptance")
    return document


async def sale_for(session, order_id):
    return await session.scalar(select(ShopFiscalDocument).where(ShopFiscalDocument.order_id == order_id,
        ShopFiscalDocument.kind == "sale", ShopFiscalDocument.status.notin_(["cancelled", "error"]))
        .order_by(ShopFiscalDocument.id.desc()))


async def return_document(session, return_id):
    return await session.scalar(select(ShopFiscalDocument).where(ShopFiscalDocument.return_id == return_id,
        ShopFiscalDocument.kind == "return", ShopFiscalDocument.status.notin_(["cancelled", "error"]))
        .order_by(ShopFiscalDocument.id.desc()))


async def active_returns(session, order_id):
    # Financial returns permanently prohibit completion as a full-price sale.
    from app.models.shop_carrier_returns import ShopCarrierReturn
    return bool(await session.scalar(select(ShopFinancialReturn.id).where(ShopFinancialReturn.order_id == order_id).limit(1))
        or await session.scalar(select(ShopCarrierReturn.id).where(ShopCarrierReturn.order_id == order_id).limit(1)))


def sale_snapshot(order, row):
    confirmation = valid_order(order)
    if not confirmation:
        fail("payment_required", "A canonical B1 payment is required")
    payment = next(p for p in order.payments if p.id == confirmation.payment_id)
    items = [{"order_item_id": i.id, "product_id": i.product_id, "name": i.product_name,
        "quantity": i.quantity, "total_minor": exact_minor(i.total_price if i.total_price is not None else i.price * i.quantity)}
        for i in sorted(order.items, key=lambda i: i.id)]
    amount = exact_minor(order.total_amount)
    additional = sum(i["total_minor"] for i in items) - amount
    if amount <= 0 or payment.amount_minor != amount or additional < 0 or any(i["total_minor"] < 0 for i in items):
        fail("invalid_original_amounts", "Exact canonical Shop totals do not reconcile")
    state = {"document_kind": "shop_mock_fiscal_receipt", "authority": "test", "simulation": True,
        "is_sent_dps": False, "tax_profile": "TEST_ONLY_NO_LEGAL_TAX_PROFILE", "notice": NOTICE,
        "order_id": order.id, "payment_confirmation_id": confirmation.id,
        "payment": {"id": payment.id, "amount_minor": amount, "method": payment.method,
                    "source": payment.source, "authority": confirmation.authority},
        "items": items, "additional_discount_minor": additional, "amount_minor": amount,
        "currency": "UAH", "register_id": REGISTER, "shift_revision": row.shift_revision}
    net_lines(state)
    return state


async def claimed_return_quantities(session, order_id):
    used = {}
    for row in (await session.scalars(select(ShopFinancialReturn).where(ShopFinancialReturn.order_id == order_id))).all():
        for line in row.lines:
            used[line["order_item_id"]] = used.get(line["order_item_id"], 0) + line["quantity"]
    return used


def allocate_return_lines(snapshot, requested, used):
    """Original discount allocation plus cumulative unit rounding, in kopecks.

    Shared by financial claims and parcel preflight so no immutable parcel can
    reserve quantities whose complete financial return would be zero.
    """
    allocated = net_lines(snapshot)
    originals = {i["order_item_id"]: i for i in snapshot["items"]}
    if len({i["order_item_id"] for i in requested}) != len(requested):
        fail("return_quantity_exceeded", "Duplicate return lines are forbidden")
    lines = []
    for requested_line in sorted(requested, key=lambda i: i["order_item_id"]):
        identifier, count = requested_line["order_item_id"], requested_line["quantity"]
        item, before = originals.get(identifier), used.get(identifier, 0)
        if not item or type(count) is not int or count <= 0 or before + count > item["quantity"]:
            fail("return_quantity_exceeded", "Return quantity exceeds the original unclaimed quantity")
        net, quantity = allocated[identifier], item["quantity"]
        amount = net * (before + count) // quantity - net * before // quantity
        lines.append({**item, "quantity": count, "total_minor": amount})
    return lines


async def return_snapshot(session, financial_return, order, row):
    original = await issued(session, await session.get(ShopFiscalDocument, financial_return.sale_document_id))
    if original.order_id != order.id:
        fail("sale_receipt_required", "Original Shop sale does not belong to this order")
    refund = await session.scalar(select(ShopRefundFact).where(ShopRefundFact.return_id == financial_return.id))
    if not refund or refund.amount_minor != financial_return.amount_minor:
        fail("refund_required", "Record the explicit test refund first")
    if row.shift_revision != financial_return.shift_revision:
        fail("shift_revision_conflict", "Return obligation belongs to its original open shift")
    return {"document_kind": "shop_mock_fiscal_receipt", "authority": "test", "simulation": True,
        "is_sent_dps": False, "tax_profile": "TEST_ONLY_NO_LEGAL_TAX_PROFILE", "notice": NOTICE,
        "order_id": order.id, "return_id": financial_return.id,
        "payment_confirmation_id": original.payment_confirmation_id,
        "original_sale_id": original.id, "original_sale_hash": original.snapshot_hash,
        "refund_id": refund.id, "items": financial_return.lines, "additional_discount_minor": 0,
        "amount_minor": financial_return.amount_minor, "currency": "UAH", "register_id": REGISTER,
        "shift_revision": row.shift_revision}


class ShopMockFiscalPolicy:
    available = True
    test_only = True

    async def authorize_in_session(self, session, **expected):
        await guard(session)
        order = await shop_sales.load_order(session, expected["order_id"])
        valid_order(order)
        row = await register(session, lock=True)
        if await active_returns(session, order.id):
            fail("active_return", "An order with a financial return cannot complete as a full sale")
        payment = shop_sales.canonical_payment(order)
        if not payment and expected["phase"] == "dispatch" and shop_sales.is_cod_order(order):
            open_shift(row)
            verification = f"shop-test-cod-deferral:{row.shift_revision}"
        else:
            document = await issued(session, await sale_for(session, order.id))
            if (not payment or document.payment_confirmation_id != payment.id
                    or document.snapshot.get("payment", {}).get("id") != expected["payment_id"]
                    or document.snapshot.get("amount_minor") != exact_minor(order.total_amount)
                    or document.snapshot.get("payment_confirmation_id") != payment.id
                    or digest(sale_snapshot(order, SimpleNamespace(shift_revision=document.shift_revision))) != document.snapshot_hash):
                fail("sale_receipt_required", "Issued receipt must bind this exact canonical payment")
            verification = f"shop-test-receipt:{document.id}:{document.snapshot_hash}"
        return shop_sales.FiscalAuthorization(**expected, verification_id=verification, authority="test")

    async def readiness(self, session, order, phase):
        if not await enabled(session):
            return ["shop_fiscal_disabled"]
        if await active_returns(session, order.id):
            return ["active_return"]
        payment = shop_sales.canonical_payment(order)
        if not payment and phase == "dispatch" and shop_sales.is_cod_order(order):
            row = await register(session)
            return [] if row and row.shift_status == "open" else ["shift_closed"]
        try:
            document = await issued(session, await sale_for(session, order.id))
            if (not payment or document.payment_confirmation_id != payment.id
                    or digest(sale_snapshot(order, SimpleNamespace(shift_revision=document.shift_revision))) != document.snapshot_hash):
                return ["sale_receipt_required"]
            return []
        except HTTPException:
            return ["sale_receipt_required"]


bound_policy = ShopMockFiscalPolicy()


class LocalShopMockAdapter:
    """Provider emulator with its own committed acceptance store, never HTTP."""
    async def submit(self, sessions, *, document_id, provider_uuid, snapshot_hash):
        async with sessions() as session:
            await guard(session)
            await register(session, lock=True)
            document = await session.get(ShopFiscalDocument, document_id)
            if (not document or document.provider_uuid != provider_uuid or document.snapshot_hash != snapshot_hash
                    or document.dispatched_at is None or document.status != "unknown"):
                fail("provider_binding_invalid", "Provider requires the committed exact dispatch fence")
            existing = await session.get(ShopFiscalAcceptance, provider_uuid)
            if existing:
                if existing.document_id != document_id or existing.snapshot_hash != snapshot_hash:
                    fail("provider_binding_invalid", "Provider UUID already has different content")
            else:
                session.add(ShopFiscalAcceptance(provider_uuid=provider_uuid, document_id=document_id,
                    snapshot_hash=snapshot_hash, accepted_at=datetime.now(UTC)))
            await session.commit()

    async def lookup(self, sessions, *, provider_uuid):
        async with sessions() as session:
            await shop_sales.require_test_database(session, "shop_fiscal_test_unsafe")
            return await session.get(ShopFiscalAcceptance, provider_uuid)


adapter = LocalShopMockAdapter()


def preview_view(row):
    return PreviewView(**{key: getattr(row, key) for key in ("id", "order_id", "return_id", "kind",
        "expected_revision", "shift_revision", "snapshot", "snapshot_hash", "expires_at")})


async def document_view(session, actor, row):
    permission = "can_fiscalize_sales" if row.kind == "sale" else "can_fiscalize_returns"
    allowed, live = permitted(actor, permission), await enabled(session)
    safe = await shop_sales.test_database_is_safe(session)
    return DocumentView(**{key: getattr(row, key) for key in ("id", "order_id", "return_id", "kind", "status",
        "revision", "provider_uuid", "shift_revision", "snapshot", "snapshot_hash", "dispatched_at", "issued_at", "created_at")},
        amount_minor=row.snapshot["amount_minor"], actions={
            "process": bool(allowed and live and row.status == "queued"),
            "reconcile": bool(allowed and safe and row.status == "unknown"),
            "cancel": bool(allowed and live and row.status == "queued" and row.dispatched_at is None),
            "print": bool(allowed and row.status == "issued"),
            **{f"replay_{name}": allowed for name in ("process", "reconcile", "cancel")}},
        blocking_reasons=[] if live else ["shop_fiscal_disabled"])


async def return_view(session, actor, row):
    refund = await session.scalar(select(ShopRefundFact).where(ShopRefundFact.return_id == row.id))
    receipt = await session.scalar(select(ShopReturnReceipt).where(ShopReturnReceipt.return_id == row.id))
    document = await return_document(session, row.id)
    order = await shop_sales.load_order(session, row.order_id)
    live = await enabled(session)
    money, tax = permitted(actor, "can_record_sales"), permitted(actor, "can_fiscalize_returns")
    inventory = permitted(actor, "can_post_inventory")
    pickup = order.fulfillment_method == "pickup"
    reasons = [] if live else ["shop_fiscal_disabled"]
    return ReturnView(**{key: getattr(row, key) for key in ("id", "order_id", "sale_document_id", "revision",
        "shift_revision", "amount_minor", "lines", "reason", "created_at", "carrier_receipt_id")},
        refund={key: getattr(refund, key) for key in ("id", "amount_minor", "method", "external_reference", "evidence", "created_at")} if refund else None,
        physical_receipt={key: getattr(receipt, key) for key in ("id", "lines", "evidence", "created_at", "carrier_receipt_id")} if receipt else None,
        fiscal_document=await document_view(session, actor, document) if document else None,
        actions={"refund": bool(live and money and not refund), "receipt_preview": bool(live and tax and refund and not document),
            "receipt": bool(live and tax and refund and not document), "physical_receipt": bool(live and inventory and (pickup or row.carrier_receipt_id) and not receipt),
            "replay_refund": money, "replay_receipt_preview": tax, "replay_receipt": tax, "replay_physical_receipt": inventory},
        blocking_reasons=reasons)


async def order_view(session, actor, order):
    confirmation = valid_order(order)
    document = await sale_for(session, order.id)
    returns = list((await session.scalars(select(ShopFinancialReturn).where(ShopFinancialReturn.order_id == order.id)
        .order_by(ShopFinancialReturn.id))).all())
    live = await enabled(session)
    tax, money = permitted(actor, "can_fiscalize_sales"), permitted(actor, "can_record_sales")
    ready_return = bool(document and document.status == "issued" and shop_sales.canonical_fulfillment(order)
        and order.fulfillment_method == "pickup")
    if document and document.status == "issued" and order.fulfillment_method == "nova_poshta":
        from app.services.shop_carrier_returns import cases, financial_capacity
        from app.models.shop_carrier_returns import ShopCarrierReturnReceipt
        for case in cases(order):
            physical = await session.scalar(select(ShopCarrierReturnReceipt).where(ShopCarrierReturnReceipt.case_id == case.id))
            if physical and case.financial_mode == "paid":
                _, _, remaining = await financial_capacity(session, order, physical.id)
                ready_return = bool(remaining)
    reasons = [] if live else ["shop_fiscal_disabled"]
    if not confirmation:
        reasons.append("payment_required")
    if confirmation and not shop_sales.canonical_fulfillment(order):
        reasons.append("pre_handover_return_blocked")
    if order.fulfillment_method == "nova_poshta" and not ready_return:
        reasons.append("carrier_receipt_required")
    return OrderFiscalView(enabled=live, order_id=order.id, revision=order.revision,
        sale_document=await document_view(session, actor, document) if document else None,
        returns=[await return_view(session, actor, row) for row in returns],
        actions={"sale_preview": bool(live and tax and confirmation and not document),
            "sale_receipt": bool(live and tax and confirmation and not document),
            "create_return": bool(live and money and ready_return),
            "replay_sale_preview": tax, "replay_sale_receipt": tax, "replay_create_return": money}, blocking_reasons=reasons)


async def reconciliation(session):
    # Called under register lock by closing commands. Never takes order locks.
    documents = list((await session.scalars(select(ShopFiscalDocument))).all())
    valid_sales = set()
    valid_returns = set()
    for document in documents:
        if document.status == "issued":
            await issued(session, document)
            (valid_sales if document.kind == "sale" else valid_returns).add(
                document.payment_confirmation_id if document.kind == "sale" else document.return_id)
    payments = list((await session.scalars(select(ShopPaymentConfirmation))).all())
    returns = list((await session.scalars(select(ShopFinancialReturn))).all())
    unresolved = []
    for row in returns:
        refund = await session.scalar(select(ShopRefundFact.id).where(ShopRefundFact.return_id == row.id))
        physical = await session.scalar(select(ShopReturnReceipt.id).where(ShopReturnReceipt.return_id == row.id))
        if not refund or row.id not in valid_returns or not physical:
            unresolved.append(row.id)
    from app.services.shop_carrier_returns import unresolved_paid_cases
    return {"pending_document_ids": [d.id for d in documents if d.status in {"queued", "unknown"}],
        "unknown_document_ids": [d.id for d in documents if d.status == "unknown"],
        "unreceipted_payment_ids": [p.id for p in payments if p.id not in valid_sales],
        "unresolved_return_ids": unresolved, "carrier_return_case_ids": await unresolved_paid_cases(session)}


def unsettled(state):
    return any(state.values())


def report_view(row):
    return ReportView(**{key: getattr(row, key) for key in ("id", "kind", "shift_revision", "totals", "reconciliation", "created_at")})


async def register_view(session, actor):
    row = await register(session)
    live, permission = await enabled(session), permitted(actor, "can_manage_fiscal_settings")
    state = await reconciliation(session) if permission else {"pending_document_ids": [], "unreceipted_payment_ids": [],
        "unresolved_return_ids": [], "unknown_document_ids": []}
    is_open = bool(row and row.shift_status == "open")
    reports = list((await session.scalars(select(ShopFiscalReport).order_by(ShopFiscalReport.id))).all()) if permission else []
    return RegisterView(enabled=live, shift_status=row.shift_status if row else "closed",
        shift_revision=row.shift_revision if row else 1, reconciliation=state if permission else None, reports=[report_view(r) for r in reports],
        actions={"open": bool(live and permission and not is_open), "close": bool(live and permission and is_open and not unsettled(state)),
            "x": bool(live and permission and is_open), "z": bool(live and permission and is_open and not unsettled(state)),
            **{f"replay_{name}": permission for name in ("open", "close", "x", "z")}},
        blocking_reasons=(["shop_fiscal_disabled"] if not live else []) + (["shift_unsettled"] if unsettled(state) else []))


async def read(session, actor, resource, resource_id=None):
    actor = await actor_for(session, actor)
    if resource == "register":
        return await register_view(session, actor)
    if resource == "order":
        return await order_view(session, actor, await shop_sales.load_order(session, resource_id))
    models = {"document": ShopFiscalDocument, "preview": ShopFiscalPreview,
              "return": ShopFinancialReturn, "report": ShopFiscalReport}
    row = await session.get(models[resource], resource_id)
    if not row:
        raise HTTPException(404, "Shop fiscal resource not found")
    if resource == "report":
        if not permitted(actor, "can_manage_fiscal_settings"):
            raise HTTPException(403, "Fiscal settings permission required")
        return report_view(row)
    order = await shop_sales.load_order(session, row.order_id)
    valid_order(order)
    if resource == "preview":
        permission = "can_fiscalize_sales" if row.kind == "sale" else "can_fiscalize_returns"
        if not permitted(actor, permission) or row.actor_id != actor.id:
            raise HTTPException(403, "Preview owner permission required")
        return preview_view(row)
    return await document_view(session, actor, row) if resource == "document" else await return_view(session, actor, row)


async def shift_command(session, actor, payload, key, operation):
    try:
        actor = await actor_for(session, actor, "can_manage_fiscal_settings")
        view = ReportView if operation in {"x", "z"} else RegisterView
        previous = await cached(session, actor, key, operation, payload, view)
        if previous:
            return previous
        await guard(session)
        command = await intent(session, actor, key, operation, payload)
        if command.response_json:
            return view.model_validate(command.response_json)
        row = await register(session, lock=True, create=True)
        if row.shift_revision != payload.expected_shift_revision:
            fail("shift_revision_conflict", "Reload the dedicated Shop shift")
        if operation == "open":
            if row.shift_status == "open":
                fail("shift_already_open", "Shift already open")
            row.shift_status, row.shift_revision = "open", row.shift_revision + 1
        else:
            open_shift(row)
            state = await reconciliation(session)
            if operation in {"close", "z"} and unsettled(state):
                fail("shift_unsettled", "Reconcile pending documents, payments and return obligations before closing")
            if operation in {"x", "z"}:
                docs = list((await session.scalars(select(ShopFiscalDocument).where(
                    ShopFiscalDocument.shift_revision == row.shift_revision, ShopFiscalDocument.status == "issued")
                    .order_by(ShopFiscalDocument.id))).all())
                for doc in docs:
                    await issued(session, doc)
                sales = [d for d in docs if d.kind == "sale"]
                returns = [d for d in docs if d.kind == "return"]
                sales_minor, returns_minor = sum(d.snapshot["amount_minor"] for d in sales), sum(d.snapshot["amount_minor"] for d in returns)
                report = ShopFiscalReport(kind=operation, shift_revision=row.shift_revision,
                    totals={"sale_count": len(sales), "return_count": len(returns), "sales_minor": sales_minor,
                        "returns_minor": returns_minor, "net_minor": sales_minor - returns_minor, "document_ids": [d.id for d in docs]},
                    reconciliation=state, operator_id=actor.id, created_at=datetime.now(UTC))
                session.add(report)
            if operation in {"close", "z"}:
                row.shift_status, row.shift_revision = "closed", row.shift_revision + 1
        await session.flush()
        result = report_view(report) if operation in {"x", "z"} else await register_view(session, actor)
        return await finish(session, actor, command, result, "shift_" + operation)
    except Exception:
        await session.rollback()
        raise


async def order_command(session, actor, order_id, payload, key, operation, *, return_id=None):
    permission = "can_record_sales" if operation == "create_return" else (
        "can_fiscalize_returns" if return_id else "can_fiscalize_sales")
    view = ReturnView if operation == "create_return" else PreviewView if operation == "preview" else DocumentView
    scope = f"{operation}:{'return:'+str(return_id) if return_id else 'order:'+str(order_id)}"
    try:
        actor = await actor_for(session, actor, permission)
        previous = await cached(session, actor, key, scope, payload, view)
        if previous:
            return previous
        await guard(session)
        order = await shop_sales.load_order(session, order_id, lock=True)
        from app.services.shop_mono_checkbox import deny_internal_issuer
        deny_internal_issuer(order)
        confirmation = valid_order(order)
        if order.fulfillment_method == "nova_poshta":
            from app.services.shop_carrier_returns import lock_shipments
            await lock_shipments(session, order)
        command = await intent(session, actor, key, scope, payload)
        if command.response_json:
            return view.model_validate(command.response_json)
        row = await register(session, lock=True, create=True)
        open_shift(row)
        financial_return = await session.get(ShopFinancialReturn, return_id, populate_existing=True) if return_id else None
        if return_id and (not financial_return or financial_return.order_id != order.id):
            raise HTTPException(404, "Return not found")
        revision(financial_return or order, payload.expected_revision)
        if operation == "create_return":
            carrier_receipt_id = None
            carrier_remaining = None
            if order.fulfillment_method == "nova_poshta":
                from app.services.shop_carrier_returns import financial_capacity
                if not payload.carrier_receipt_id:
                    fail("carrier_receipt_required", "Receive the canonical reverse parcel before financial return")
                carrier_case, physical, carrier_remaining = await financial_capacity(session, order, payload.carrier_receipt_id, requested=payload.lines)
                if carrier_case.shift_revision != row.shift_revision or carrier_case.sale_document_id != payload.sale_document_id:
                    fail("shift_revision_conflict", "Financial return must resolve the exact assigned parcel shift and original sale")
                carrier_receipt_id = physical.id
            elif payload.carrier_receipt_id is not None:
                fail("carrier_receipt_required", "Pickup financial return cannot use a carrier receipt")
            if not shop_sales.canonical_fulfillment(order):
                fail("pre_handover_return_blocked", "Paid cancellation before handover requires a dedicated future cancellation workflow")
            original = await issued(session, await session.get(ShopFiscalDocument, payload.sale_document_id))
            if original.kind != "sale" or original.order_id != order.id or original.payment_confirmation_id != confirmation.id:
                fail("sale_receipt_required", "Return must bind this canonical Shop sale")
            used = await claimed_return_quantities(session, order.id)
            lines = allocate_return_lines(original.snapshot, [i.model_dump() for i in payload.lines], used)
            amount = sum(i["total_minor"] for i in lines)
            if amount <= 0:
                fail("unsupported_zero_total", "A paid financial return must have a positive amount")
            if carrier_remaining:
                requested = {i.order_item_id: i.quantity for i in payload.lines}
                tail = [{"order_item_id": i["order_item_id"], "quantity": i["quantity"] - requested.get(i["order_item_id"], 0)}
                    for i in carrier_remaining if i["quantity"] > requested.get(i["order_item_id"], 0)]
                used_after = {identifier: count + requested.get(identifier, 0) for identifier, count in used.items()}
                used_after.update({identifier: used.get(identifier, 0) + count for identifier, count in requested.items()})
                if tail and sum(i["total_minor"] for i in allocate_return_lines(original.snapshot, tail, used_after)) == 0:
                    fail("unsupported_zero_total", "Bundle remaining zero-value parcel quantities with this positive financial claim")
            financial_return = ShopFinancialReturn(order_id=order.id, sale_document_id=original.id,
                carrier_receipt_id=carrier_receipt_id,
                shift_revision=row.shift_revision, revision=1, amount_minor=amount, lines=lines, reason=payload.reason,
                operator_id=actor.id, created_at=datetime.now(UTC))
            session.add(financial_return)
            order.revision += 1
            await session.flush()
            result = await return_view(session, actor, financial_return)
        else:
            existing = await return_document(session, return_id) if return_id else await sale_for(session, order_id)
            if existing:
                fail("document_exists", "Recover the existing receipt instead of creating another")
            state = await return_snapshot(session, financial_return, order, row) if return_id else sale_snapshot(order, row)
            if operation == "preview":
                preview = ShopFiscalPreview(order_id=order.id, return_id=return_id, actor_id=actor.id,
                    kind="return" if return_id else "sale", expected_revision=payload.expected_revision,
                    shift_revision=row.shift_revision, snapshot=state, snapshot_hash=digest(state),
                    expires_at=datetime.now(UTC) + timedelta(minutes=10))
                session.add(preview)
                await session.flush()
                result = preview_view(preview)
            else:
                preview = await session.get(ShopFiscalPreview, payload.preview_id)
                if not preview or preview.actor_id != actor.id or preview.order_id != order.id or preview.return_id != return_id:
                    fail("preview_stale", "Preview owner and order must match")
                if aware(preview.expires_at) <= datetime.now(UTC):
                    fail("preview_expired", "Create a fresh approval preview")
                if (preview.expected_revision != payload.expected_revision or preview.shift_revision != row.shift_revision
                        or preview.snapshot_hash != digest(preview.snapshot) or preview.snapshot_hash != digest(state)):
                    fail("preview_stale", "Amounts, payment, return and shift must match the approved preview")
                if await session.scalar(select(ShopFiscalDocument.id).where(ShopFiscalDocument.preview_id == preview.id)):
                    fail("preview_consumed", "Preview has already been used")
                document = ShopFiscalDocument(order_id=order.id, payment_confirmation_id=state["payment_confirmation_id"],
                    return_id=return_id, preview_id=preview.id, actor_id=actor.id, kind=preview.kind, status="queued",
                    revision=1, provider_uuid=str(uuid4()), shift_revision=row.shift_revision, snapshot=state,
                    snapshot_hash=preview.snapshot_hash, created_at=datetime.now(UTC))
                session.add(document)
                if financial_return:
                    financial_return.revision += 1
                else:
                    order.revision += 1
                await session.flush()
                result = await document_view(session, actor, document)
        return await finish(session, actor, command, result, operation, order.id)
    except Exception:
        await session.rollback()
        raise


async def return_command(session, actor, return_id, payload, key, operation):
    try:
        actor = await actor_for(session, actor, "can_post_inventory" if operation == "physical_receipt" else "can_record_sales")
        scope = f"return:{return_id}:{operation}"
        previous = await cached(session, actor, key, scope, payload, ReturnView)
        if previous:
            return previous
        await guard(session)
        financial_return = await session.get(ShopFinancialReturn, return_id)
        if not financial_return:
            raise HTTPException(404, "Return not found")
        order = await shop_sales.load_order(session, financial_return.order_id, lock=True)
        valid_order(order)
        if order.fulfillment_method == "nova_poshta":
            from app.services.shop_carrier_returns import lock_shipments
            await lock_shipments(session, order)
        command = await intent(session, actor, key, scope, payload)
        if command.response_json:
            return ReturnView.model_validate(command.response_json)
        row = await register(session, lock=True)
        open_shift(row)
        await session.refresh(financial_return)
        revision(financial_return, payload.expected_revision)
        if financial_return.shift_revision != row.shift_revision:
            fail("shift_revision_conflict", "Return obligation belongs to another shift")
        sale = await issued(session, await session.get(ShopFiscalDocument, financial_return.sale_document_id))
        fulfillment = shop_sales.canonical_fulfillment(order)
        if not fulfillment:
            fail("pre_handover_return_blocked", "Original canonical handover is required")
        if operation == "refund":
            if await session.scalar(select(ShopRefundFact.id).where(ShopRefundFact.return_id == return_id)):
                fail("refund_exists", "Refund is already recorded")
            if await session.scalar(select(ShopRefundFact.id).where(ShopRefundFact.external_reference == payload.external_reference)):
                fail("external_reference_used", "Refund reference already belongs to a return")
            session.add(ShopRefundFact(return_id=return_id, amount_minor=financial_return.amount_minor,
                method=sale.snapshot["payment"]["method"], external_reference=payload.external_reference,
                evidence=payload.evidence, operator_id=actor.id, created_at=datetime.now(UTC)))
        else:
            if await session.scalar(select(ShopReturnReceipt.id).where(ShopReturnReceipt.return_id == return_id)):
                fail("physical_receipt_exists", "Physical receipt is already recorded")
            if fulfillment.kind == "nova_poshta":
                from app.services.shop_carrier_returns import link_financial_receipt
                receipt_id = await link_financial_receipt(session, order, financial_return, payload)
                session.add(ShopReturnReceipt(return_id=return_id, carrier_receipt_id=receipt_id,
                    lines=[i.model_dump() for i in sorted(payload.lines, key=lambda i: i.order_item_id)],
                    evidence=payload.evidence, operator_id=actor.id, created_at=datetime.now(UTC)))
                financial_return.revision += 1
                await session.flush()
                return await finish(session, actor, command, await return_view(session, actor, financial_return), operation, order.id)
            requested = {i.order_item_id: i for i in payload.lines}
            if (len(requested) != len(payload.lines) or {k: i.quantity for k, i in requested.items()}
                    != {i["order_item_id"]: i["quantity"] for i in financial_return.lines}):
                fail("receipt_quantity_mismatch", "Physical receipt must cover every reserved return line exactly")
            originals = {i["order_item_id"]: i for i in fulfillment.snapshot["items"]}
            products = {p.id: p for p in (await session.scalars(select(Product).where(Product.id.in_(
                sorted({i["product_id"] for i in financial_return.lines}))).order_by(Product.id)
                .with_for_update().execution_options(populate_existing=True))).all()}
            for line in financial_return.lines:
                item = originals.get(line["order_item_id"])
                shipped = await session.scalar(select(func.coalesce(func.sum(-InventoryMovement.on_hand_delta), 0)).where(
                    InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == line["order_item_id"],
                    InventoryMovement.product_id == line["product_id"], InventoryMovement.movement_type == InventoryMovementType.shipment,
                    InventoryMovement.reference_key == f'order:{order.id}:item:{line["order_item_id"]}:shipment'))
                restored = await session.scalar(select(func.coalesce(func.sum(InventoryMovement.on_hand_delta), 0)).where(
                    InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == line["order_item_id"],
                    InventoryMovement.product_id == line["product_id"], InventoryMovement.movement_type == InventoryMovementType.customer_return))
                if (not item or item["product_id"] != line["product_id"] or item["quantity"] != shipped
                        or restored + line["quantity"] > shipped or line["product_id"] not in products):
                    fail("shipped_quantity_exceeded", "Return requires the exact original canonical handover ledger")
                if requested[line["order_item_id"]].disposition == "restock":
                    products[line["product_id"]].stock_quantity += line["quantity"]
                    session.add(InventoryMovement(product_id=line["product_id"], movement_type=InventoryMovementType.customer_return,
                        quantity=line["quantity"], on_hand_delta=line["quantity"], reserved_delta=0, order_id=order.id,
                        order_item_id=line["order_item_id"], performed_by_user_id=actor.id,
                        reference_key=f'shop-return:{return_id}:{line["order_item_id"]}', reason=payload.evidence))
            session.add(ShopReturnReceipt(return_id=return_id, lines=[i.model_dump() for i in sorted(payload.lines, key=lambda i: i.order_item_id)],
                evidence=payload.evidence, operator_id=actor.id, created_at=datetime.now(UTC)))
        financial_return.revision += 1
        await session.flush()
        result = await return_view(session, actor, financial_return)
        return await finish(session, actor, command, result, operation, order.id)
    except IntegrityError as error:
        await session.rollback()
        fail("external_reference_used", "Refund or physical receipt already has an accepted fact")
    except Exception:
        await session.rollback()
        raise


async def document_command(session, actor, document_id, payload, key, operation):
    try:
        document = await session.get(ShopFiscalDocument, document_id)
        if not document:
            raise HTTPException(404, "Document not found")
        permission = "can_fiscalize_sales" if document.kind == "sale" else "can_fiscalize_returns"
        actor = await actor_for(session, actor, permission)
        actor_id = actor.id
        scope = f"document:{document_id}:{operation}"
        previous = await cached(session, actor, key, scope, payload, DocumentView)
        if previous:
            return previous
        await shop_sales.require_test_database(session, "shop_fiscal_test_unsafe")
        # Only an undispatched create depends on the new-work flag and origin.
        # Accepted external truth remains recoverable after either is revoked.
        if operation == "process" and document.dispatched_at is None:
            await guard(session)
            await actor_for(session, type("Actor", (), {"id": document.actor_id})(), permission)
        elif operation == "cancel":
            await guard(session)
        order = await shop_sales.load_order(session, document.order_id, lock=True)
        valid_order(order)
        command = await intent(session, actor, key, scope, payload)
        if command.response_json:
            return DocumentView.model_validate(command.response_json)
        row = await register(session, lock=True)
        document = await session.scalar(select(ShopFiscalDocument).where(ShopFiscalDocument.id == document_id)
            .with_for_update().execution_options(populate_existing=True))
        revision(document, payload.expected_revision)
        if document.status == "issued":
            await issued(session, document)
            return await finish(session, actor, command, await document_view(session, actor, document), operation, order.id)
        if operation == "cancel":
            if document.dispatched_at is not None or document.status != "queued":
                fail("document_dispatched", "A dispatched or terminal receipt cannot be cancelled")
            document.status = "cancelled"
            document.revision += 1
            audit(session, actor, "receipt_cancelled", document_id=document.id, evidence=payload.evidence)
            return await finish(session, actor, command, await document_view(session, actor, document), operation, order.id)
        dispatch = operation == "process" and document.status == "queued" and document.dispatched_at is None
        if dispatch:
            await guard(session)
            open_shift(row)
            if row.shift_revision != document.shift_revision:
                fail("shift_revision_conflict", "Receipt approval belongs to another shift")
            financial_return = await session.get(ShopFinancialReturn, document.return_id) if document.return_id else None
            current = await return_snapshot(session, financial_return, order, row) if financial_return else sale_snapshot(order, row)
            if document.snapshot_hash != digest(current) or document.snapshot_hash != digest(document.snapshot):
                fail("preview_stale", "Approved immutable snapshot no longer matches its exact basis")
            document.dispatched_at, document.status = datetime.now(UTC), "unknown"
            document.revision += 1
            audit(session, actor, "receipt_dispatch_fence", document_id=document.id, provider_uuid=document.provider_uuid)
            # A durable exact acknowledgment exists before the provider boundary.
            # GET hydrates the later projection; concurrent same-key callers see
            # this same response even if provider acceptance is already visible.
            await session.flush()
            acknowledgment = await document_view(session, actor, document)
            command.order_id, command.response_json = order.id, acknowledgment.model_dump(mode="json")
            await session.commit()  # fence and intent persist BEFORE the independent provider transaction
        elif document.status != "unknown":
            fail("document_unknown", "Only unknown dispatched receipts can be reconciled")
        document_uuid, snapshot_hash, order_id = document.provider_uuid, document.snapshot_hash, order.id
        sessions = async_sessionmaker(session.bind, expire_on_commit=False)
        # A create is attempted at most once. Lost response is deliberately left
        # unknown; explicit reconciliation looks up the same independently saved UUID.
        timed_out = False
        if dispatch:
            try:
                await adapter.submit(sessions, document_id=document_id, provider_uuid=document_uuid, snapshot_hash=snapshot_hash)
            except Exception:
                timed_out = True
        truth = None if timed_out else await adapter.lookup(sessions, provider_uuid=document_uuid)
        await session.rollback()
        actor = await actor_for(session, type("Actor", (), {"id": actor_id})(), permission)
        order = await shop_sales.load_order(session, order_id, lock=True)
        await register(session, lock=True)
        document = await session.scalar(select(ShopFiscalDocument).where(ShopFiscalDocument.id == document_id)
            .with_for_update().execution_options(populate_existing=True))
        command = await intent(session, actor, key, scope, payload)
        if truth:
            if truth.document_id != document.id or truth.snapshot_hash != document.snapshot_hash:
                fail("provider_binding_invalid", "Provider acceptance does not match exact receipt")
            if document.status != "issued":
                document.status, document.issued_at = "issued", truth.accepted_at
                document.revision += 1
        result = DocumentView.model_validate(command.response_json) if command.response_json else await document_view(session, actor, document)
        return await finish(session, actor, command, result, "receipt_" + operation, order.id)
    except Exception:
        await session.rollback()
        raise


async def print_document(session, actor, document_id):
    row = await session.get(ShopFiscalDocument, document_id)
    if not row:
        raise HTTPException(404, "Document not found")
    await actor_for(session, actor, "can_fiscalize_sales" if row.kind == "sale" else "can_fiscalize_returns")
    await issued(session, row)
    items = "".join(f'<li>{escape(str(i.get("name") or ""))} × {i["quantity"]}: {i["total_minor"] / 100:.2f} UAH</li>' for i in row.snapshot["items"])
    return f'<!doctype html><html lang="uk"><meta charset="utf-8"><title>Shop TEST</title><body><h1>{escape(NOTICE)}</h1><p>{escape(row.provider_uuid)}</p><ul>{items}</ul><p>{row.snapshot["amount_minor"] / 100:.2f} UAH</p></body></html>'
