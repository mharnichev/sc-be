"""Physical direct-prepayment NP returns through an injected, pinned HTTP mock."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import OrderIntent, OrderStatus
from app.models.product import Product
from app.models.shipment import Shipment
from app.models.shop_np_direct_returns import (
    ShopNPDirectReturnCase as Case,
    ShopNPDirectReturnObservation as Observation,
    ShopNPDirectReturnReceipt as Receipt,
)
from app.models.shop_np_returns import ShopNPReturnCase
from app.models.shop_sales import ShopOperationAudit
from app.schemas import shop_np_direct_returns as dto
from app.services import shop_np_returns as np_returns, shop_payment_resolutions as resolutions, shop_sales as sales
from app.services.np_return_provider import NPReturnError, NPReturnGateway
from app.services.order_documents import claim_intent, digest, operation_key

fail = sales.fail
_DELIVERY_CODES = {"9", "10", "11"}


def gateway():
    return None


async def enabled_gateway(session):
    provider = gateway()
    if (not settings.shop_order_operations_enabled or not settings.shop_np_returns_enabled
            or not settings.shop_mono_checkbox_enabled or type(provider) is not NPReturnGateway
            or type(provider.transport) is not httpx.MockTransport or not provider.available
            or provider.profile["environment"] != "test"):
        fail("np_direct_return_unavailable", "An isolated Nova Poshta return profile is required", 503)
    await sales.require_test_database(session, "np_direct_return_test_unsafe")
    return provider


async def case_for(session, order_id):
    return await session.scalar(select(Case).where(Case.order_id == order_id))


async def receipt_for(session, order_id):
    return await session.scalar(select(Receipt).where(Receipt.order_id == order_id))


async def latest_receipt_id(session, order_id) -> str | None:
    receipt_id = await session.scalar(select(Receipt.id).where(Receipt.order_id == order_id))
    return str(receipt_id) if receipt_id else None


async def observations_for(session, case):
    return list(await session.scalars(
        select(Observation).where(Observation.case_id == case.id).order_by(Observation.id)
    ))


async def original_context(session, order):
    from app.services import shop_mono_checkbox_refunds as refunds

    await resolutions.financial_review_guard(session, order.id)
    if (not sales.eligible_order(order) or order.fulfillment_method != "nova_poshta"
            or order.status not in {OrderStatus.paid, OrderStatus.completed}
            or resolutions.cases(order) or order.shop_np_returns or order.shop_carrier_returns
            or order.shop_fiscal_documents or order.shop_financial_returns
            or order.shop_checkbox_documents or order.shop_checkbox_returns or order.qr_payment_attempts):
        fail("np_direct_return_ineligible", "An exclusive direct-prepayment Nova Poshta order is required")
    payment = sales.canonical_payment(order)
    if not payment or payment.snapshot.get("original_payment_method") != "onlineCard":
        fail("np_direct_return_sale_required", "The canonical original direct prepayment is required")
    sale, _ = refunds.original(order)
    fulfillment = sales.canonical_fulfillment(order)
    shipment = order.shipments[0] if len(order.shipments) == 1 else None
    if (not fulfillment or fulfillment.kind != "nova_poshta" or not shipment
            or shipment.id != fulfillment.shipment_id or shipment.simulation or not shipment.active
            or shipment.carrier != "nova_poshta" or shipment.source != "attach" or shipment.return_of_shipment_id
            or not shipment.handed_over_at or not shipment.tracking_number
            or shipment.tracking_number != shipment.provider_reference
            or order.tracking_number not in {None, shipment.tracking_number}
            or {item.order_item_id: item.quantity for item in shipment.shipment_items}
            != {item.id: item.quantity for item in order.items}):
        fail("np_direct_return_dispatch_required", "The exact original attached and dispatched parcel is required")
    return shipment, fulfillment, sale


def binding(order, shipment, fulfillment, sale):
    return {
        "order_id": order.id,
        "original_shipment_id": shipment.id,
        "original_ttn": shipment.tracking_number,
        "fulfillment_id": fulfillment.id,
        "fulfillment_hash": fulfillment.snapshot_hash,
        "dispatched_at": sales.aware(shipment.handed_over_at).isoformat(),
        "sale_id": sale.id,
        "sale_hash": sale.snapshot_hash,
    }


def verify_binding(case, expected):
    if (case.snapshot_hash != digest(case.snapshot)
            or any(case.snapshot.get(key) != value for key, value in expected.items())
            or case.order_id != expected["order_id"] or case.original_shipment_id != expected["original_shipment_id"]
            or case.fulfillment_id != expected["fulfillment_id"] or case.sale_id != expected["sale_id"]
            or case.original_ttn != expected["original_ttn"] or case.return_ttn != case.snapshot.get("return_ttn")):
        fail("np_direct_return_binding_conflict", "Stored physical return requires reconciliation")


def assert_profile(payload, provider, case):
    profile_hash = digest(provider.profile)
    if (payload.expected_profile_id != provider.profile["id"] or payload.expected_profile_hash != profile_hash
            or case and case.snapshot["profile"] != provider.profile):
        fail("np_return_profile_conflict", "Reload the original Nova Poshta return profile")


def validate_pair(proof, provider, case, previous):
    expected = case.snapshot
    np_returns.validate_proof(proof, provider, case.original_ttn, case.return_ttn, expected["linkage_kind"], "paid",
                              previous=previous)
    original = proof["original"]
    dispatched = np_returns.date(expected["dispatched_at"])
    event = np_returns.date(original.get("event_at"))
    if event < dispatched - timedelta(seconds=30):
        fail("np_direct_return_original_proof", "The original carrier event must follow dispatch")
    terminal_codes = _DELIVERY_CODES | {"103"}
    prior = previous[0].proof["original"] if previous else original
    if (prior["raw_status"] not in terminal_codes
            or original["raw_status"] not in {prior["raw_status"], "102"}
            and not (prior["raw_status"] in _DELIVERY_CODES and original["raw_status"] in _DELIVERY_CODES)):
        fail("np_direct_return_original_proof", "A dated original delivery or refusal is required")
    if original["raw_status"] == "102" and not previous:
        fail("np_direct_return_original_proof", "A returning status requires earlier delivery or refusal proof")
    if (previous and previous[-1].proof["reverse"]["status"] == "delivered"
            and proof["reverse"]["status"] != "delivered"):
        fail("np_return_proof_invalid", "A delivered reverse parcel cannot regress")
    if proof["reverse"]["status"] == "delivered":
        reverse_event = np_returns.date(proof["reverse"].get("event_at"))
        if reverse_event < np_returns.date(prior["event_at"]) - timedelta(seconds=30):
            fail("np_return_proof_invalid", "Reverse delivery must follow the original delivery or refusal")
    return proof


def receipt_ready(observation):
    if not observation or observation.proof_hash != digest(observation.proof):
        fail("np_return_proof_invalid", "An exact carrier observation is required")
    proof = observation.proof
    now = datetime.now(UTC)
    if not now - timedelta(seconds=min(settings.np_tracking_stale_seconds, 7200)) <= np_returns.date(
            proof["observed_at"]) <= now + timedelta(seconds=30):
        fail("np_return_tracking_stale", "Refresh carrier tracking before receiving contents")
    reverse = proof["reverse"]
    if reverse["raw_status"] not in _DELIVERY_CODES or reverse["status"] != "delivered" or not reverse["event_at"]:
        fail("np_return_delivery_required", "A dated delivered reverse parcel is required before content inspection")


def claim_lines(order, lines):
    selected = {line.order_item_id: line.quantity for line in lines}
    dispatched = {item.id: item.quantity for item in order.items}
    if len(selected) != len(lines) or any(quantity > dispatched.get(item_id, 0) for item_id, quantity in selected.items()):
        fail("np_return_quantity_mismatch", "Select distinct quantities from the original dispatched positions")
    return {str(item_id): selected[item_id] for item_id in sorted(selected)}


def received_lines(case, lines):
    selected = {str(line.order_item_id): line for line in lines}
    claimed = case.snapshot["claimed"]
    if (len(selected) != len(lines) or selected.keys() != claimed.keys()
            or any(line.received_quantity > claimed[key] for key, line in selected.items())):
        fail("np_return_quantity_mismatch", "Inspect each claimed position and record its actual received quantity")
    return [line.model_dump() for line in sorted(lines, key=lambda line: line.order_item_id)]


def receipt_snapshot(receipt, case, observation, lines, evidence):
    return {
        "receipt_id": str(receipt.id),
        "order_id": case.order_id,
        "case_id": str(case.id),
        "case_hash": case.snapshot_hash,
        "observation_id": observation.id,
        "observation_hash": observation.proof_hash,
        "original_ttn": case.original_ttn,
        "return_ttn": case.return_ttn,
        "operator_id": receipt.operator_id,
        "lines": lines,
        "evidence": evidence,
        "contents_verified": True,
        "authority": "test",
    }


def quantities(lines):
    return {str(line["order_item_id"]): line["received_quantity"] for line in lines if line["received_quantity"]}


def movement_key(receipt_id, item_id):
    return f"shop-np-direct-return:{receipt_id}:{item_id}"


async def received_stock(session, order, receipt, lines, *, post=False):
    by_id = {item.id: item for item in order.items}
    rows = list(await session.scalars(select(InventoryMovement).where(
        (InventoryMovement.order_id == order.id) | (InventoryMovement.order_item_id.in_(by_id)),
        InventoryMovement.movement_type.in_([InventoryMovementType.shipment, InventoryMovementType.customer_return]),
    )))
    movements = {row.reference_key: row for row in rows}
    returned = {row.reference_key for row in rows if row.movement_type == InventoryMovementType.customer_return}
    expected = {movement_key(receipt.id, line["order_item_id"]) for line in lines if line["restock_quantity"]}
    if returned != (set() if post else expected):
        fail("np_direct_return_stock_conflict", "Existing returned stock requires reconciliation")
    products = {}
    if post and expected:
        product_ids = {by_id[line["order_item_id"]].product_id for line in lines if line["restock_quantity"]}
        products = {p.id: p for p in await session.scalars(select(Product).where(Product.id.in_(product_ids))
                    .order_by(Product.id).with_for_update().execution_options(populate_existing=True))}
    for line in lines:
        item = by_id[line["order_item_id"]]
        shipped = movements.get(f"order:{order.id}:item:{item.id}:shipment")
        if (not shipped or shipped.movement_type != InventoryMovementType.shipment or shipped.order_id != order.id
                or shipped.order_item_id != item.id or shipped.product_id != item.product_id
                or shipped.quantity != item.quantity or shipped.on_hand_delta != -item.quantity
                or shipped.reserved_delta != -item.quantity):
            fail("np_direct_return_stock_conflict", "The exact original shipped stock ledger is required")
        restored = line["restock_quantity"]
        if not restored:
            continue
        key = movement_key(receipt.id, item.id)
        if post:
            products[item.product_id].stock_quantity += restored
            session.add(InventoryMovement(
                product_id=item.product_id, movement_type=InventoryMovementType.customer_return,
                quantity=restored, on_hand_delta=restored, reserved_delta=0, order_id=order.id,
                order_item_id=item.id, performed_by_user_id=receipt.operator_id, reference_key=key,
                reason=receipt.snapshot["evidence"],
            ))
        else:
            recorded = movements[key]
            if (recorded.order_id, recorded.order_item_id, recorded.product_id, recorded.quantity,
                    recorded.on_hand_delta, recorded.reserved_delta, recorded.performed_by_user_id) != (
                    order.id, item.id, item.product_id, restored, restored, 0, receipt.operator_id):
                fail("np_direct_return_stock_conflict", "The exact inspected restock ledger is required")


async def financial_basis(session, order, receipt_id: UUID) -> dict:
    """Return exact accepted physical quantities without creating a monetary claim."""
    await sales.require_test_database(session, "np_direct_return_test_unsafe")
    order = await sales.load_order(session, order.id, lock=True)
    shipment, fulfillment, sale = await original_context(session, order)
    receipt = await session.get(Receipt, UUID(str(receipt_id)))
    if not receipt or receipt.order_id != order.id:
        raise HTTPException(404, "Nova Poshta direct return receipt not found")
    case = await session.get(Case, receipt.case_id)
    verify_binding(case, binding(order, shipment, fulfillment, sale))
    observation = await session.get(Observation, receipt.observation_id)
    if (not observation or observation.case_id != case.id or observation.proof_hash != digest(observation.proof)
            or receipt.proof_hash != digest(receipt.snapshot)
            or receipt.snapshot != receipt_snapshot(receipt, case, observation, receipt.snapshot["lines"],
                                                     receipt.snapshot["evidence"])
            or receipt.quantities != quantities(receipt.snapshot["lines"])):
        fail("np_return_proof_invalid", "Stored physical receipt requires reconciliation")
    if not receipt.quantities:
        fail("np_direct_return_no_received_goods", "No received goods are eligible for a financial claim")
    await received_stock(session, order, receipt, receipt.snapshot["lines"])
    return {
        "receipt_id": str(receipt.id), "quantities": dict(receipt.quantities), "proof_hash": receipt.proof_hash,
        "original_ttn": case.original_ttn, "return_ttn": case.return_ttn,
    }


async def view(session, order, actor, *, case=None, previous=None, receipt=None):
    provider = shipment = None
    reasons = []
    try:
        provider = await enabled_gateway(session)
        shipment, fulfillment, sale = await original_context(session, order)
        if case:
            verify_binding(case, binding(order, shipment, fulfillment, sale))
            if case.snapshot["profile"] != provider.profile:
                fail("np_return_profile_conflict", "Restore the original Nova Poshta return profile")
    except HTTPException as error:
        reasons.append(error.detail["code"])
    observation = previous[-1] if previous else None
    available = not reasons
    arrived = False
    if case and not receipt:
        try:
            receipt_ready(observation)
            arrived = True
        except HTTPException as error:
            reasons.append(error.detail["code"])
    manage = actor.is_superuser or actor.can_manage_shipments
    inventory = actor.is_superuser or actor.can_post_inventory
    return dto.ReturnView(
        order_id=order.id, revision=order.revision, available=available, case_id=case.id if case else None,
        profile_id=provider.profile["id"] if provider else None,
        profile_hash=digest(provider.profile) if provider else None,
        original_shipment_id=case.original_shipment_id if case else shipment.id if shipment else None,
        original_ttn=case.original_ttn if case else shipment.tracking_number if shipment else None,
        return_ttn=case.return_ttn if case else None,
        status="received" if receipt else "arrived" if arrived else "awaiting_return" if case else "not_attached",
        eligible_lines=[dto.ReturnLine(order_item_id=item.id, quantity=item.quantity) for item in order.items]
        if shipment else [],
        claimed_lines=[dto.ReturnLine(order_item_id=int(item_id), quantity=quantity)
                       for item_id, quantity in case.snapshot["claimed"].items()] if case else [],
        observation=observation.proof if observation else None,
        receipt=dto.ReceiptView(id=receipt.id, proof_hash=receipt.proof_hash, quantities=receipt.quantities,
                                lines=receipt.snapshot["lines"], evidence=receipt.snapshot["evidence"],
                                operator_id=receipt.operator_id, created_at=receipt.created_at) if receipt else None,
        financial_refund_path=f"/api/v1/backoffice/shop-direct-refunds/orders/{order.id}"
        if receipt and receipt.quantities and (actor.is_superuser or actor.can_record_sales) else None,
        can_attach=bool(available and not case and manage),
        can_refresh=bool(available and case and not receipt and manage),
        can_receive=bool(available and arrived and manage and inventory),
        blocking_reasons=reasons,
    )


async def read(session, actor, order_id):
    actor = await np_returns.actor_for(session, actor)
    order = await sales.load_order(session, order_id)
    case = await case_for(session, order_id)
    previous = await observations_for(session, case) if case else None
    return await view(session, order, actor, case=case, previous=previous, receipt=await receipt_for(session, order_id))


async def reserve_ttn(session, return_ttn):
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("np-return-ttn:" + return_ttn, 0))))
    if (await session.scalar(select(Shipment.id).where(
            (Shipment.tracking_number == return_ttn) | (Shipment.provider_reference == return_ttn)).limit(1))
            or await session.scalar(select(ShopNPReturnCase.id).where(ShopNPReturnCase.return_ttn == return_ttn))
            or await session.scalar(select(Case.id).where(Case.return_ttn == return_ttn))):
        fail("np_return_linkage_mismatch", "This reverse tracking number is already assigned")


async def command(session, actor, order_id, payload, raw_key, operation):
    from app.services import shop_mono_checkbox_refunds as refunds

    actor_id = actor.id
    key = operation_key(raw_key, f"shop-np-direct-return:{actor_id}:{operation}:{order_id}")
    request_hash = digest(payload.model_dump(mode="json"))
    try:
        actor = await np_returns.actor_for(session, actor, operation)
        await sales.require_test_database(session, "np_direct_return_test_unsafe")
        replay = await session.get(OrderIntent, key)
        if replay and replay.payload_hash != request_hash:
            fail("idempotency_conflict", "Key already used for another request")
        if replay and replay.response_json:
            return dto.ReturnView.model_validate(replay.response_json)

        provider = await enabled_gateway(session)
        order = await sales.load_order(session, order_id)
        shipment, _, _ = await original_context(session, order)
        case = await case_for(session, order_id)
        assert_profile(payload, provider, case)
        if operation == "attach" and payload.original_shipment_id != shipment.id:
            fail("np_direct_return_binding_conflict", "Select the canonical original dispatch")
        if operation != "attach" and not case:
            raise HTTPException(404, "Nova Poshta direct return case not found")
        proof = None
        if operation in {"attach", "refresh"}:
            original_ttn = shipment.tracking_number
            return_ttn = payload.return_ttn if operation == "attach" else case.return_ttn
            linkage_kind = payload.linkage_kind if operation == "attach" else case.snapshot["linkage_kind"]
            if original_ttn == return_ttn:
                fail("np_return_linkage_mismatch", "The reverse tracking number must be distinct")
            await session.rollback()
            try:
                proof = await provider.verify_pair(original_ttn, return_ttn, linkage_kind=linkage_kind)
            except NPReturnError as error:
                fail(error.code, "Nova Poshta could not verify the linked parcel pair")
            actor = await np_returns.actor_for(session, SimpleNamespace(id=actor_id), operation)

        order = await sales.load_order(session, order_id, lock=True)
        intent = await claim_intent(session, key, request_hash)
        if intent.response_json:
            return dto.ReturnView.model_validate(intent.response_json)
        current_provider = await enabled_gateway(session)
        if current_provider.profile != provider.profile:
            fail("np_return_profile_conflict", "Nova Poshta return profile changed during verification")
        shipment, fulfillment, sale = await original_context(session, order)
        expected = binding(order, shipment, fulfillment, sale)
        case = await case_for(session, order_id)
        assert_profile(payload, current_provider, case)
        if order.revision != payload.expected_revision:
            fail("revision_conflict", "Reload the current return resource")
        if await refunds.active(session, order_id):
            fail("np_direct_return_financial_active", "The direct financial claim already exists")
        receipt = await receipt_for(session, order_id)
        if receipt:
            fail("np_return_receipt_exists", "Parcel contents are already recorded")
        previous = await observations_for(session, case) if case else []
        now = datetime.now(UTC)

        if operation == "attach":
            if case or shipment.id != payload.original_shipment_id or shipment.tracking_number != original_ttn:
                fail("np_direct_return_binding_conflict", "The selected original dispatch has changed")
            await reserve_ttn(session, return_ttn)
            manual = payload.manual_link
            if manual and (manual.order_id, manual.original_shipment_id, manual.original_ttn, manual.return_ttn) != (
                    order.id, shipment.id, original_ttn, return_ttn):
                fail("np_return_manual_link_invalid", "Documentary linkage must identify the exact order and parcel pair")
            snapshot = expected | {
                "return_ttn": return_ttn, "claimed": claim_lines(order, payload.lines),
                "profile": provider.profile, "authority": "test", "linkage_kind": linkage_kind,
                "manual_link": manual.model_dump(mode="json") if manual else None,
            }
            case = Case(id=uuid4(), order_id=order.id, original_shipment_id=shipment.id,
                        fulfillment_id=fulfillment.id, sale_id=sale.id, original_ttn=original_ttn,
                        return_ttn=return_ttn, snapshot=snapshot, snapshot_hash=digest(snapshot),
                        operator_id=actor.id, created_at=now)
            session.add(case)
            await session.flush()
        else:
            verify_binding(case, expected)

        if proof is not None:
            validate_pair(proof, provider, case, previous)
            observation = Observation(case_id=case.id, proof=proof, proof_hash=digest(proof),
                                      operator_id=actor.id, created_at=now)
            session.add(observation)
            previous.append(observation)
        else:
            observation = previous[-1] if previous else None
            receipt_ready(observation)
            lines = received_lines(case, payload.lines)
            receipt = Receipt(id=uuid4(), case_id=case.id, order_id=order.id, observation_id=observation.id,
                              quantities=quantities(lines), operator_id=actor.id, created_at=now)
            receipt.snapshot = receipt_snapshot(receipt, case, observation, lines, payload.evidence)
            receipt.proof_hash = digest(receipt.snapshot)
            session.add(receipt)
            await session.flush()
            await received_stock(session, order, receipt, lines, post=True)

        order.revision += 1
        session.add(ShopOperationAudit(order_id=order.id, operator_id=actor.id, request_key=key,
                                      operation=f"np_direct_return_{operation}",
                                      facts={"revision": order.revision, "case_id": str(case.id)}, created_at=now))
        await session.flush()
        result = await view(session, order, actor, case=case, previous=previous, receipt=receipt)
        intent.order_id, intent.response_json = order.id, result.model_dump(mode="json")
        await enabled_gateway(session)
        await session.commit()
        return result
    except IntegrityError:
        await session.rollback()
        fail("np_direct_return_conflict", "The reverse parcel or physical receipt conflicts with recorded facts")
    except Exception:
        await session.rollback()
        raise
