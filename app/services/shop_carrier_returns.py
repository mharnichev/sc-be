"""Explicit local Shop carrier returns. Never calls Nova Poshta or moves money."""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.admin_user import AdminUser
from app.models.order import OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentItem, TrackingEvent
from app.models.shop_carrier_returns import (ShopCarrierReturn, ShopCarrierReturnAcceptance,
    ShopCarrierReturnReceipt, ShopCarrierUnpaidClosure)
from app.models.shop_fiscal import ShopFinancialReturn, ShopFiscalDocument, ShopReturnReceipt, ShopRefundFact
from app.schemas.shop_carrier_returns import CarrierCaseView, CarrierStateView
from app.services import shop_sales, shop_fiscal, sales_access
from app.services.order_documents import digest

fail, aware = shop_sales.fail, shop_sales.aware
CARRIER, SOURCE = "shop_mock_np_return", "shop_return"


async def guard(session):
    if not settings.shop_carrier_returns_mock_enabled:
        fail("shop_carrier_returns_disabled", "Local Shop carrier returns are disabled", 503)
    await shop_sales.require_test_database(session, "shop_carrier_return_test_unsafe")


async def enabled(session):
    return settings.shop_carrier_returns_mock_enabled and await shop_sales.test_database_is_safe(session)


async def actor_for(session, actor, operation="read"):
    actor = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id).with_for_update(read=True)
        .execution_options(populate_existing=True))
    if actor:
        await sales_access.deny_master(session, actor)
    read = any(shop_fiscal.permitted(actor, field) for field in ("can_manage_shipments", "can_record_sales",
        "can_post_inventory", "can_fiscalize_sales", "can_fiscalize_returns"))
    if not read or (operation != "read" and not shop_fiscal.permitted(actor, "can_manage_shipments")):
        raise HTTPException(403, "Current Shop carrier return permission required")
    for field in (("can_post_inventory",) if operation == "receive" else
                  ("can_post_inventory", "can_record_sales") if operation == "close_unpaid" else ()):
        if not shop_fiscal.permitted(actor, field):
            raise HTTPException(403, "Current carrier return inventory/sales permission required")
    return actor


def valid_binding(case, order=None, reverse=None):
    if not case or case.authority != "test" or digest(case.snapshot) != case.snapshot_hash:
        return False
    expected = {"order_id": case.order_id, "original_shipment_id": case.original_shipment_id,
        "return_shipment_id": case.return_shipment_id, "fulfillment_id": case.fulfillment_id,
        "sale_document_id": case.sale_document_id, "shift_revision": case.shift_revision,
        "financial_mode": case.financial_mode, "authority": "test", "provider_uuid": case.provider_uuid, "lines": case.lines}
    if case.snapshot != expected:
        return False
    fulfillment = case.__dict__.get("fulfillment")
    if (not fulfillment or fulfillment.order_id != case.order_id or fulfillment.id != case.fulfillment_id
            or fulfillment.kind != "nova_poshta" or fulfillment.shipment_id != case.original_shipment_id
            or fulfillment.snapshot_hash != digest(fulfillment.snapshot)):
        return False
    if order is not None:
        if order.id != case.order_id or shop_sales.canonical_fulfillment(order) is None:
            return False
        original_lines = {i.id: (i.product_id, i.quantity) for i in order.items}
        if (len({i["order_item_id"] for i in case.lines}) != len(case.lines) or not case.lines
                or any(i["order_item_id"] not in original_lines or type(i["quantity"]) is not int
                    or not 0 < i["quantity"] <= original_lines[i["order_item_id"]][1]
                    or i["product_id"] != original_lines[i["order_item_id"]][0] for i in case.lines)):
            return False
    if reverse is not None and (reverse.id != case.return_shipment_id or reverse.order_id != case.order_id
            or not reverse.simulation or reverse.carrier != CARRIER or reverse.source != SOURCE
            or reverse.return_of_shipment_id != case.original_shipment_id or reverse.operation_uuid != case.provider_uuid
            or {i.order_item_id: i.quantity for i in reverse.shipment_items} != {i["order_item_id"]: i["quantity"] for i in case.lines}):
        return False
    return True


def shipment_case_id(shipment):
    case = shipment.__dict__.get("shop_carrier_return_case")
    return case.id if valid_binding(case, reverse=shipment) else None


def cases(order):
    return [case for case in order.__dict__.get("shop_carrier_returns", []) if valid_binding(case, order)]


def active_case(order):
    return bool(cases(order))


def projection(order):
    rows = cases(order)
    if not rows:
        return None
    case = rows[-1]
    if case.status == "unknown":
        return "simulated_carrier_return_unknown"
    receipt, closure = case.__dict__.get("receipt"), case.__dict__.get("closure")
    if (case.status == "closed_unpaid" and closure and valid_receipt(receipt, case)
            and closure.snapshot_hash == digest(closure.snapshot) and closure.order_id == order.id
            and closure.case_id == case.id and closure.receipt_id == receipt.id
            and closure.snapshot == {"order_id": order.id, "case_id": case.id, "receipt_id": receipt.id,
                "case_hash": case.snapshot_hash, "receipt_hash": receipt.snapshot_hash, "authority": "test"}
            and case.financial_mode == "unpaid_cod" and not order.payments
            and order.status == OrderStatus.cancelled):
        return "simulated_unpaid_refusal_closed"
    if valid_receipt(receipt, case):
        return "simulated_carrier_return_received"
    return "simulated_carrier_returned" if case.status == "returned" else "simulated_carrier_returning"


async def lock_shipments(session, order):
    return list((await session.scalars(select(Shipment).where(Shipment.order_id == order.id).order_by(Shipment.id)
        .with_for_update().execution_options(populate_existing=True))).all())


async def load_case(session, order_id, case_id):
    case = await session.scalar(select(ShopCarrierReturn).where(ShopCarrierReturn.id == case_id,
        ShopCarrierReturn.order_id == order_id).execution_options(populate_existing=True))
    if not case:
        raise HTTPException(404, "Shop carrier return not found")
    return case


def validate_original(order, rows):
    shop_fiscal.valid_order(order)
    fulfillment = shop_sales.canonical_fulfillment(order)
    if not fulfillment or fulfillment.kind != "nova_poshta":
        fail("handover_required", "Canonical full B1 carrier dispatch is required")
    original = next((s for s in rows if s.id == fulfillment.shipment_id), None)
    if (not original or original.simulation or original.carrier != "nova_poshta" or original.source != "attach"
            or original.return_of_shipment_id is not None or not original.active or not original.tracking_number
            or original.provider_reference != original.tracking_number or not original.handed_over_at):
        fail("carrier_return_binding_invalid", "Original parcel must match the exact canonical B1 dispatch")
    for row in rows:
        if row.id != original.id:
            case = row.__dict__.get("shop_carrier_return_case")
            if not valid_binding(case, order, row) or case.original_shipment_id != original.id:
                fail("carrier_shipment_ambiguous", "Unbranded or unrelated extra shipment requires reconciliation")
    if {i.order_item_id: i.quantity for i in original.shipment_items} != {i.id: i.quantity for i in order.items}:
        fail("carrier_allocation_mismatch", "Original outbound parcel must cover the complete order")
    return original, fulfillment


async def accepted_event(session, shipment, statuses):
    checked = aware(shipment.last_checked_at)
    if (shipment.last_attempt_status != "success" or not checked
            or checked < datetime.now(UTC) - timedelta(seconds=settings.np_tracking_stale_seconds)
            or checked > datetime.now(UTC) + timedelta(seconds=30)):
        fail("carrier_return_tracking_stale", "A fresh successful dated carrier observation is required")
    if shipment.tracking_status not in statuses:
        fail("carrier_return_refusal_required" if "refused" in statuses else "carrier_return_not_ready", "Carrier status does not authorize this return operation")
    event = await session.scalar(select(TrackingEvent).where(TrackingEvent.shipment_id == shipment.id,
        TrackingEvent.normalized_status == shipment.tracking_status, TrackingEvent.event_at == shipment.provider_event_at,
        TrackingEvent.raw_status == shipment.raw_status, TrackingEvent.observed_at <= shipment.last_checked_at)
        .order_by(TrackingEvent.id.desc()))
    if not event or not aware(event.event_at) or aware(event.event_at) > checked + timedelta(seconds=30):
        fail("carrier_return_not_ready", "Matching accepted dated carrier event is required")


async def receive_ready(session, case, reverse):
    truth = await session.get(ShopCarrierReturnAcceptance, case.provider_uuid)
    if (not truth or truth.case_id != case.id or truth.snapshot_hash != case.snapshot_hash
            or truth.tracking_number != reverse.tracking_number or reverse.provider_reference != truth.tracking_number):
        fail("carrier_return_binding_invalid", "Reverse TTN lacks exact independently accepted Shop provenance")
    if not valid_binding(case, reverse=reverse):
        fail("carrier_return_binding_invalid", "Reverse parcel identity changed")
    await accepted_event(session, reverse, {"returned"})


def valid_receipt(receipt, case):
    if (not receipt or receipt.case_id != case.id or receipt.snapshot_hash != digest(receipt.snapshot)
            or receipt.snapshot.get("case_id") != case.id or receipt.snapshot.get("case_hash") != case.snapshot_hash
            or receipt.snapshot.get("authority") != "test" or receipt.snapshot.get("lines") != receipt.lines):
        return False
    return (len({i["order_item_id"] for i in receipt.lines}) == len(receipt.lines)
        and {i["order_item_id"]: i["quantity"] for i in receipt.lines} == {i["order_item_id"]: i["quantity"] for i in case.lines}
        and all(i.get("disposition") in {"restock", "not_restock"} for i in receipt.lines))


async def financial_capacity(session, order, receipt_id, *, requested=None):
    receipt = await session.get(ShopCarrierReturnReceipt, receipt_id)
    case = await load_case(session, order.id, receipt.case_id) if receipt else None
    if not case or not valid_binding(case, order) or case.financial_mode != "paid" or not valid_receipt(receipt, case):
        fail("carrier_receipt_required", "An accepted paid Shop carrier return physical receipt is required")
    used = {}
    for row in (await session.scalars(select(ShopFinancialReturn).where(ShopFinancialReturn.carrier_receipt_id == receipt.id))).all():
        for line in row.lines:
            used[line["order_item_id"]] = used.get(line["order_item_id"], 0) + line["quantity"]
    physical = {i["order_item_id"]: i for i in receipt.lines}
    remaining = [{**line, "quantity": line["quantity"] - used.get(line["order_item_id"], 0)}
        for line in case.lines if line["quantity"] > used.get(line["order_item_id"], 0)]
    if requested is not None and (len({i.order_item_id for i in requested}) != len(requested)
            or any(i.order_item_id not in physical or i.quantity + used.get(i.order_item_id, 0) > physical[i.order_item_id]["quantity"] for i in requested)):
        fail("carrier_receipt_quantity_exceeded", "Financial claims exceed unallocated received return quantities")
    return case, receipt, remaining


async def link_financial_receipt(session, order, financial_return, payload):
    receipt = await session.get(ShopCarrierReturnReceipt, financial_return.carrier_receipt_id) if financial_return.carrier_receipt_id else None
    case = await load_case(session, order.id, receipt.case_id) if receipt else None
    if not case or not valid_binding(case, order) or not valid_receipt(receipt, case) or case.financial_mode != "paid":
        fail("carrier_receipt_required", "Financial return must link its exact accepted Shop parcel receipt")
    requested = {i.order_item_id: i for i in payload.lines}
    if len(requested) != len(payload.lines) or {k: i.quantity for k, i in requested.items()} != {i["order_item_id"]: i["quantity"] for i in financial_return.lines}:
        fail("receipt_quantity_mismatch", "Cover every financial return quantity exactly")
    physical = {i["order_item_id"]: i for i in receipt.lines}
    used = {}
    for linked in (await session.scalars(select(ShopReturnReceipt).where(ShopReturnReceipt.carrier_receipt_id == receipt.id))).all():
        for line in linked.lines:
            used[line["order_item_id"]] = used.get(line["order_item_id"], 0) + line["quantity"]
    for key, item in requested.items():
        if key not in physical or physical[key]["disposition"] != item.disposition:
            fail("carrier_receipt_disposition_mismatch", "Disposition must match the original physical receipt")
        if item.quantity + used.get(key, 0) > physical[key]["quantity"]:
            fail("carrier_receipt_quantity_exceeded", "Received quantity has already been linked")
    return receipt.id


async def unresolved_paid_cases(session):
    result = []
    for case in (await session.scalars(select(ShopCarrierReturn).where(ShopCarrierReturn.financial_mode == "paid"))).all():
        receipt = await session.scalar(select(ShopCarrierReturnReceipt).where(ShopCarrierReturnReceipt.case_id == case.id))
        if not valid_binding(case) or not valid_receipt(receipt, case):
            result.append(case.id)
            continue
        rows = list((await session.scalars(select(ShopFinancialReturn).where(ShopFinancialReturn.carrier_receipt_id == receipt.id))).all())
        claimed = {}
        done = True
        for row in rows:
            for line in row.lines:
                claimed[line["order_item_id"]] = claimed.get(line["order_item_id"], 0) + line["quantity"]
            document = await shop_fiscal.return_document(session, row.id)
            refund = await session.scalar(select(ShopRefundFact.id).where(ShopRefundFact.return_id == row.id))
            link = await session.scalar(select(ShopReturnReceipt).where(ShopReturnReceipt.return_id == row.id))
            if not refund or not link or link.carrier_receipt_id != receipt.id or not document or document.status != "issued":
                done = False
            else:
                await shop_fiscal.issued(session, document)
        if claimed != {i["order_item_id"]: i["quantity"] for i in case.lines} or not done:
            result.append(case.id)
    return result


@dataclass(frozen=True)
class ReturnObservation:
    case_id: int
    provider_uuid: str
    tracking_number: str
    snapshot_hash: str
    status: str
    raw_status: str
    event_at: datetime


class LocalShopReturnAdapter:
    """Server-owned deterministic emulator. No client-selected outcomes/HTTP."""
    async def submit(self, sessions, *, case_id, provider_uuid, snapshot_hash):
        async with sessions() as session:
            await guard(session)
            case = await session.get(ShopCarrierReturn, case_id)
            if not valid_binding(case) or case.provider_uuid != provider_uuid or case.snapshot_hash != snapshot_hash or case.status != "unknown":
                fail("carrier_return_binding_invalid", "Committed exact return dispatch is required")
            await shop_fiscal.register(session, lock=True)
            existing = await session.get(ShopCarrierReturnAcceptance, provider_uuid)
            if not existing:
                session.add(ShopCarrierReturnAcceptance(provider_uuid=provider_uuid, case_id=case.id,
                    snapshot_hash=snapshot_hash, tracking_number="SHOP-RETURN-" + provider_uuid.replace("-", ""), accepted_at=datetime.now(UTC)))
            elif existing.case_id != case.id or existing.snapshot_hash != snapshot_hash:
                fail("carrier_return_binding_invalid", "Return UUID is already bound to different data")
            await session.commit()

    async def lookup(self, sessions, *, provider_uuid):
        async with sessions() as session:
            await shop_sales.require_test_database(session, "shop_carrier_return_test_unsafe")
            return await session.get(ShopCarrierReturnAcceptance, provider_uuid)

    async def observe(self, sessions, *, case_id, provider_uuid):
        async with sessions() as session:
            await guard(session)
            case = await session.get(ShopCarrierReturn, case_id)
            truth = await session.get(ShopCarrierReturnAcceptance, provider_uuid)
            if not valid_binding(case) or not truth or truth.case_id != case_id or truth.snapshot_hash != case.snapshot_hash:
                fail("carrier_return_unknown", "Reconcile the original return UUID first")
            return ReturnObservation(case_id, provider_uuid, truth.tracking_number, truth.snapshot_hash,
                "returned", "SHOP_TEST_RETURNED", datetime.now(UTC))


adapter = LocalShopReturnAdapter()


async def case_view(session, actor, order, case):
    if not valid_binding(case, order):
        fail("carrier_return_binding_invalid", "Stored return provenance requires reconciliation")
    reverse = next((s for s in order.shipments if s.id == case.return_shipment_id), None)
    if not reverse or not valid_binding(case, order, reverse):
        fail("carrier_return_binding_invalid", "Reverse shipment does not match its canonical case")
    receipt = await session.scalar(select(ShopCarrierReturnReceipt).where(ShopCarrierReturnReceipt.case_id == case.id))
    closure = await session.scalar(select(ShopCarrierUnpaidClosure).where(ShopCarrierUnpaidClosure.case_id == case.id))
    live, safe = await enabled(session), await shop_sales.test_database_is_safe(session)
    inventory = shop_fiscal.permitted(actor, "can_post_inventory")
    sales = shop_fiscal.permitted(actor, "can_record_sales")
    manage = shop_fiscal.permitted(actor, "can_manage_shipments")
    reasons = [] if live else ["shop_carrier_returns_disabled"]
    ready = False
    if not receipt and case.status != "unknown":
        try:
            await receive_ready(session, case, reverse)
            ready = True
        except HTTPException as error:
            reasons.append(error.detail["code"])
    remaining = []
    if receipt and case.financial_mode == "paid":
        _, _, remaining = await financial_capacity(session, order, receipt.id)
    return CarrierCaseView(**{k: getattr(case, k) for k in ("id", "order_id", "revision", "original_shipment_id",
        "return_shipment_id", "provider_uuid", "status", "financial_mode", "shift_revision", "sale_document_id", "lines")},
        order_revision=order.revision, tracking_number=reverse.tracking_number,
        observation={"status": reverse.tracking_status, "event_at": reverse.provider_event_at,
            "observed_at": reverse.last_checked_at, "last_attempt_status": reverse.last_attempt_status} if reverse.last_checked_at else None,
        receipt={k: getattr(receipt, k) for k in ("id", "lines", "evidence", "created_at")} if receipt else None,
        closure={k: getattr(closure, k) for k in ("id", "evidence", "created_at")} if closure else None,
        financial_remaining_lines=remaining, actions={"refresh": bool(live and manage and not receipt and case.status != "unknown"),
            "reconcile": bool(safe and manage and case.status == "unknown"), "receive": bool(live and manage and inventory and ready and not receipt),
            "close_unpaid": bool(live and manage and inventory and sales and receipt and case.financial_mode == "unpaid_cod" and not closure),
            "create_financial_return": bool(sales and await shop_fiscal.enabled(session) and remaining),
            "replay_refresh": manage, "replay_reconcile": manage, "replay_receive": manage and inventory, "replay_close_unpaid": manage and inventory and sales},
        blocking_reasons=reasons)


async def read(session, actor, order_id, case_id=None):
    actor = await actor_for(session, actor)
    order = await shop_sales.load_order(session, order_id)
    if case_id:
        return await case_view(session, actor, order, await load_case(session, order_id, case_id))
    rows = list((await session.scalars(select(Shipment).where(Shipment.order_id == order_id).order_by(Shipment.id))).all())
    reasons, original, fulfillment = [], None, None
    try:
        original, fulfillment = validate_original(order, rows)
    except HTTPException as error:
        reasons.append(error.detail["code"])
    found = list((await session.scalars(select(ShopCarrierReturn).where(ShopCarrierReturn.order_id == order.id).order_by(ShopCarrierReturn.id))).all())
    live = await enabled(session)
    if not live:
        reasons.append("shop_carrier_returns_disabled")
    if found:
        reasons.append("carrier_return_exists")
    confirmation = shop_sales.canonical_payment(order)
    mode = "paid" if confirmation else "unpaid_cod" if shop_sales.is_cod_order(order) else None
    if not mode:
        reasons.append("payment_required")
    if mode == "paid":
        document = await shop_fiscal.sale_for(session, order.id)
        try:
            await shop_fiscal.issued(session, document)
        except HTTPException:
            reasons.append("sale_receipt_required")
        register = await shop_fiscal.register(session)
        if not await shop_fiscal.enabled(session) or not register or register.shift_status != "open":
            reasons.append("shift_closed")
    elif original:
        try:
            await accepted_event(session, original, {"refused", "returning"})
        except HTTPException as error:
            reasons.append(error.detail["code"])
    return CarrierStateView(order_id=order.id, revision=order.revision, enabled=live,
        original_shipment_id=original.id if original else None, financial_mode=mode,
        eligible_lines=[{"order_item_id": i.id, "product_id": i.product_id, "name": i.product_name, "quantity": i.quantity}
            for i in order.items] if original else [], cases=[await case_view(session, actor, order, case) for case in found],
        actions={"create": bool(not reasons and shop_fiscal.permitted(actor, "can_manage_shipments")),
            "replay_create": shop_fiscal.permitted(actor, "can_manage_shipments")}, blocking_reasons=reasons)


async def create(session, actor, order_id, payload, key):
    try:
        actor = await actor_for(session, actor, "create")
        actor_id = actor.id
        scope = f"carrier-create:{order_id}"
        previous = await shop_fiscal.cached(session, actor, key, scope, payload, CarrierCaseView)
        if previous:
            return previous
        await guard(session)
        order = await shop_sales.load_order(session, order_id, lock=True)
        rows = await lock_shipments(session, order)
        original, fulfillment = validate_original(order, rows)
        command = await shop_fiscal.intent(session, actor, key, scope, payload)
        if command.response_json:
            return CarrierCaseView.model_validate(command.response_json)
        shop_fiscal.revision(order, payload.expected_revision)
        if original.id != payload.original_shipment_id:
            fail("carrier_return_binding_invalid", "Return must reference the exact B1 outbound parcel")
        if await session.scalar(select(ShopCarrierReturn.id).where(ShopCarrierReturn.original_shipment_id == original.id)):
            fail("carrier_return_exists", "Recover the existing return case")
        confirmation = shop_sales.canonical_payment(order)
        sale, shift = None, None
        register = await shop_fiscal.register(session, lock=True, create=bool(confirmation))
        if confirmation:
            await shop_fiscal.guard(session)
            shop_fiscal.open_shift(register)
            sale = await shop_fiscal.issued(session, await shop_fiscal.sale_for(session, order.id))
            if sale.payment_confirmation_id != confirmation.id:
                fail("sale_receipt_required", "Return requires this exact canonical paid sale")
            mode, shift = "paid", register.shift_revision
        else:
            if not shop_sales.is_cod_order(order) or order.payments or order.qr_payment_attempts or shop_sales.facts(order)[2]:
                fail("carrier_return_unpaid_facts_conflict", "Unpaid refusal requires COD without any financial or completion facts")
            if await session.scalar(select(ShopFiscalDocument.id).where(ShopFiscalDocument.order_id == order.id)) or await session.scalar(select(ShopFinancialReturn.id).where(ShopFinancialReturn.order_id == order.id)):
                fail("carrier_return_unpaid_facts_conflict", "Unpaid refusal cannot have fiscal or financial return facts")
            await accepted_event(session, original, {"refused", "returning"})
            mode = "unpaid_cod"
        requested = {i.order_item_id: i.quantity for i in payload.lines}
        originals = {i.id: i for i in order.items}
        if (len(requested) != len(payload.lines) or any(k not in originals or q > originals[k].quantity for k, q in requested.items())):
            fail("carrier_return_quantity_exceeded", "Return quantities must belong to the exact original parcel")
        if mode == "unpaid_cod" and requested != {i.id: i.quantity for i in order.items}:
            fail("carrier_return_unpaid_full_required", "Unpaid refusal must return the full original order")
        lines = [{"order_item_id": k, "product_id": originals[k].product_id, "name": originals[k].product_name, "quantity": q} for k, q in sorted(requested.items())]
        if sale:
            financial_lines = shop_fiscal.allocate_return_lines(sale.snapshot, lines,
                await shop_fiscal.claimed_return_quantities(session, order.id))
            if sum(i["total_minor"] for i in financial_lines) <= 0:
                fail("unsupported_zero_total", "Choose return quantities with a positive total; bundle zero-value units with a positive claim")
        provider_uuid, now = str(uuid4()), datetime.now(UTC)
        reverse = Shipment(order_id=order.id, simulation=True, carrier=CARRIER, source=SOURCE, active=True,
            operation_uuid=provider_uuid, provider_reference="SHOP-RETURN-" + provider_uuid.replace("-", ""),
            tracking_number=None, status="unknown", raw_status="SHOP_TEST_UNKNOWN", progress=0,
            address_snapshot=original.address_snapshot, packaging={"test_only": True, "cod_amount_minor": 0},
            return_of_shipment_id=original.id, return_evidence=payload.evidence, return_operator_id=actor.id, created_at=now)
        session.add(reverse)
        await session.flush()
        session.add_all([ShipmentItem(shipment_id=reverse.id, order_item_id=i["order_item_id"], quantity=i["quantity"]) for i in lines])
        snapshot = {"order_id": order.id, "original_shipment_id": original.id, "return_shipment_id": reverse.id,
            "fulfillment_id": fulfillment.id, "sale_document_id": sale.id if sale else None, "shift_revision": shift,
            "financial_mode": mode, "authority": "test", "provider_uuid": provider_uuid, "lines": lines}
        case = ShopCarrierReturn(**{k: snapshot[k] for k in ("order_id", "original_shipment_id", "return_shipment_id",
            "fulfillment_id", "sale_document_id", "shift_revision", "financial_mode", "authority", "provider_uuid", "lines")},
            status="unknown", revision=1, snapshot=snapshot, snapshot_hash=digest(snapshot), evidence=payload.evidence,
            operator_id=actor.id, created_at=now)
        session.add(case)
        order.revision += 1
        await session.flush()
        case_id = case.id
        case = await load_case(session, order_id, case_id)
        order = await shop_sales.load_order(session, order_id)
        acknowledgment = await case_view(session, actor, order, case)
        command.order_id, command.response_json = order_id, acknowledgment.model_dump(mode="json")
        shop_fiscal.audit(session, actor, "carrier_dispatch_fence", case_id=case_id, order_id=order_id, provider_uuid=provider_uuid)
        await session.commit()
        sessions = async_sessionmaker(session.bind, expire_on_commit=False)
        try:
            await adapter.submit(sessions, case_id=case_id, provider_uuid=provider_uuid, snapshot_hash=case.snapshot_hash)
        except Exception:
            return acknowledgment
        try:
            await reconcile_projection(session, actor_id, order_id, case_id, sessions)
            await session.commit()
        except Exception:
            await session.rollback()  # accepted ACK remains durable; current authorized staff can reconcile
        return acknowledgment
    except IntegrityError:
        await session.rollback()
        fail("carrier_return_exists", "A return parcel or tracking identity is already assigned")
    except Exception:
        await session.rollback()
        raise


async def reconcile_projection(session, actor_id, order_id, case_id, sessions):
    case = await load_case(session, order_id, case_id)
    provider_uuid = case.provider_uuid
    await session.rollback()
    truth = await adapter.lookup(sessions, provider_uuid=provider_uuid)
    actor = await actor_for(session, SimpleNamespace(id=actor_id), "reconcile")
    await shop_sales.require_test_database(session, "shop_carrier_return_test_unsafe")
    order = await shop_sales.load_order(session, order_id, lock=True)
    rows = await lock_shipments(session, order)
    await shop_fiscal.register(session, lock=True)
    case = await load_case(session, order_id, case_id)
    reverse = next((s for s in rows if s.id == case.return_shipment_id), None)
    if not reverse or not valid_binding(case, order, reverse):
        fail("carrier_return_binding_invalid", "Stored reverse identity changed")
    if truth:
        if truth.case_id != case.id or truth.snapshot_hash != case.snapshot_hash or truth.tracking_number != reverse.provider_reference:
            fail("carrier_return_binding_invalid", "Provider truth does not match the exact case")
        if case.status == "unknown":
            reverse.tracking_number, reverse.status = truth.tracking_number, "returning"
            case.status, case.revision = "returning", case.revision + 1
            shop_fiscal.audit(session, actor, "carrier_reconciled", case_id=case.id)
    await session.flush()
    return actor, order, case, reverse


async def command(session, actor, order_id, case_id, payload, key, operation):
    try:
        actor = await actor_for(session, actor, operation)
        actor_id, scope = actor.id, f"carrier:{case_id}:{operation}"
        # The accepted intent is scoped to the globally unique case. Validate
        # the enclosing route order before replaying its cached response.
        await load_case(session, order_id, case_id)
        previous = await shop_fiscal.cached(session, actor, key, scope, payload, CarrierCaseView)
        if previous:
            return previous
        if operation == "reconcile":
            await shop_sales.require_test_database(session, "shop_carrier_return_test_unsafe")
        else:
            await guard(session)
        case = await load_case(session, order_id, case_id)
        sessions = async_sessionmaker(session.bind, expire_on_commit=False)
        observation, observation_failed = None, False
        if operation == "refresh":
            provider_uuid = case.provider_uuid
            await session.rollback()
            try:
                observation = await adapter.observe(sessions, case_id=case_id, provider_uuid=provider_uuid)
            except Exception:
                observation_failed = True
            actor = await actor_for(session, SimpleNamespace(id=actor_id), operation)
        order = await shop_sales.load_order(session, order_id, lock=True)
        rows = await lock_shipments(session, order)
        await shop_fiscal.register(session, lock=True)
        case = await load_case(session, order_id, case_id)
        reverse = next((s for s in rows if s.id == case.return_shipment_id), None)
        if not reverse or not valid_binding(case, order, reverse):
            fail("carrier_return_binding_invalid", "Stored reverse case does not match this order")
        intent = await shop_fiscal.intent(session, actor, key, scope, payload)
        if intent.response_json:
            return CarrierCaseView.model_validate(intent.response_json)
        shop_fiscal.revision(case, payload.expected_revision)
        if operation == "reconcile":
            if case.status != "unknown":
                fail("carrier_return_unknown", "Only the original unknown dispatch can be reconciled")
            await session.commit()  # release business locks before independent lookup
            actor, order, case, reverse = await reconcile_projection(session, actor_id, order_id, case_id, sessions)
            intent = await shop_fiscal.intent(session, actor, key, scope, payload)
        elif operation == "refresh":
            if case.status not in {"returning", "returned"}:
                fail("carrier_return_not_ready", "Reconcile the reverse parcel before refresh")
            now = datetime.now(UTC)
            reverse.last_attempt_at = now
            if observation_failed:
                reverse.last_attempt_status = "error"
            else:
                if (not isinstance(observation, ReturnObservation) or observation.case_id != case.id
                        or observation.provider_uuid != case.provider_uuid or observation.tracking_number != reverse.tracking_number
                        or observation.snapshot_hash != case.snapshot_hash or observation.status not in {"returning", "returned"}
                        or not observation.raw_status or not aware(observation.event_at)
                        or aware(observation.event_at) > now + timedelta(seconds=30)
                        or (reverse.provider_event_at and aware(observation.event_at) < aware(reverse.provider_event_at))
                        or (case.status == "returned" and observation.status != "returned")):
                    fail("carrier_return_binding_invalid", "Server observation must match exact reverse parcel and monotonic dated state")
                reverse.last_attempt_status, reverse.last_checked_at = "success", now
                reverse.status = reverse.tracking_status = case.status = observation.status
                reverse.raw_status, reverse.provider_event_at = observation.raw_status, observation.event_at
                event_key = digest([case.id, observation.raw_status, observation.status, aware(observation.event_at).isoformat()])
                if not await session.scalar(select(TrackingEvent.id).where(TrackingEvent.event_key == event_key)):
                    session.add(TrackingEvent(shipment_id=reverse.id, event_key=event_key, raw_status=observation.raw_status,
                        normalized_status=observation.status, event_at=observation.event_at, observed_at=now))
            case.revision += 1
        elif operation == "receive":
            if await session.scalar(select(ShopCarrierReturnReceipt.id).where(ShopCarrierReturnReceipt.case_id == case.id)):
                fail("physical_receipt_exists", "This parcel has already been physically received")
            await receive_ready(session, case, reverse)
            requested = {i.order_item_id: i for i in payload.lines}
            if len(requested) != len(payload.lines) or {k: i.quantity for k, i in requested.items()} != {i["order_item_id"]: i["quantity"] for i in case.lines}:
                fail("receipt_quantity_mismatch", "Receive every chosen return quantity exactly once")
            products = {p.id: p for p in (await session.scalars(select(Product).where(Product.id.in_(sorted({i["product_id"] for i in case.lines})))
                .order_by(Product.id).with_for_update().execution_options(populate_existing=True))).all()}
            for line in case.lines:
                shipped = await session.scalar(select(func.coalesce(func.sum(-InventoryMovement.on_hand_delta), 0)).where(
                    InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == line["order_item_id"],
                    InventoryMovement.product_id == line["product_id"], InventoryMovement.movement_type == InventoryMovementType.shipment,
                    InventoryMovement.reference_key == f'order:{order.id}:item:{line["order_item_id"]}:shipment'))
                restored = await session.scalar(select(func.coalesce(func.sum(InventoryMovement.on_hand_delta), 0)).where(
                    InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == line["order_item_id"],
                    InventoryMovement.product_id == line["product_id"], InventoryMovement.movement_type == InventoryMovementType.customer_return))
                original = next(i for i in order.items if i.id == line["order_item_id"])
                if shipped != original.quantity or restored + line["quantity"] > shipped or line["product_id"] not in products:
                    fail("shipped_quantity_exceeded", "Physical return must match original B1 shipment ledger capacity")
            lines = [i.model_dump() for i in sorted(payload.lines, key=lambda i: i.order_item_id)]
            snapshot = {"case_id": case.id, "case_hash": case.snapshot_hash, "authority": "test", "lines": lines}
            receipt = ShopCarrierReturnReceipt(case_id=case.id, lines=lines, snapshot=snapshot, snapshot_hash=digest(snapshot),
                evidence=payload.evidence, operator_id=actor.id, created_at=datetime.now(UTC))
            session.add(receipt)
            await session.flush()
            for line in case.lines:
                if requested[line["order_item_id"]].disposition == "restock":
                    products[line["product_id"]].stock_quantity += line["quantity"]
                    session.add(InventoryMovement(product_id=line["product_id"], movement_type=InventoryMovementType.customer_return,
                        quantity=line["quantity"], on_hand_delta=line["quantity"], reserved_delta=0, order_id=order.id,
                        order_item_id=line["order_item_id"], performed_by_user_id=actor.id,
                        reference_key=f'shop-carrier-return:{receipt.id}:{line["order_item_id"]}', reason=payload.evidence))
            case.status, case.revision = "received", case.revision + 1
            order.revision += 1
        elif operation == "close_unpaid":
            receipt = await session.scalar(select(ShopCarrierReturnReceipt).where(ShopCarrierReturnReceipt.case_id == case.id))
            if case.financial_mode != "unpaid_cod" or not valid_receipt(receipt, case):
                fail("carrier_return_not_received", "Unpaid close requires a full accepted physical receipt")
            if order.payments or order.qr_payment_attempts or any(shop_sales.facts(order)[i] for i in (0, 2)):
                fail("carrier_return_unpaid_facts_conflict", "Unpaid closure cannot have money or completion facts")
            if await session.scalar(select(ShopFinancialReturn.id).where(ShopFinancialReturn.order_id == order.id)) or await session.scalar(select(ShopFiscalDocument.id).where(ShopFiscalDocument.order_id == order.id)):
                fail("carrier_return_unpaid_facts_conflict", "Unpaid closure cannot have fiscal or financial returns")
            if {i["order_item_id"]: i["quantity"] for i in case.lines} != {i.id: i.quantity for i in order.items}:
                fail("carrier_return_unpaid_full_required", "Unpaid closure requires the complete original parcel")
            if await session.scalar(select(ShopCarrierUnpaidClosure.id).where(ShopCarrierUnpaidClosure.case_id == case.id)):
                fail("carrier_return_closed", "Unpaid refusal already closed")
            snapshot = {"order_id": order.id, "case_id": case.id, "receipt_id": receipt.id,
                "case_hash": case.snapshot_hash, "receipt_hash": receipt.snapshot_hash, "authority": "test"}
            session.add(ShopCarrierUnpaidClosure(case_id=case.id, order_id=order.id, receipt_id=receipt.id,
                snapshot=snapshot, snapshot_hash=digest(snapshot), evidence=payload.evidence,
                operator_id=actor.id, created_at=datetime.now(UTC)))
            await session.flush()  # narrow DB cancellation exception requires this fact in the same transaction
            order.status, order.revision = OrderStatus.cancelled, order.revision + 1
            case.status, case.revision = "closed_unpaid", case.revision + 1
        else:
            raise ValueError("Unsupported carrier return command")
        await session.flush()
        order = await shop_sales.load_order(session, order_id)
        case = await load_case(session, order_id, case_id)
        result = await case_view(session, actor, order, case)
        return await shop_fiscal.finish(session, actor, intent, result, "carrier_" + operation, order.id)
    except Exception:
        await session.rollback()
        raise
