"""Explicit local mock money and parcel facts; no provider or notification I/O."""
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.models.admin_user import AdminUser
from app.models.fiscal import MockReturnReceipt, ReturnRecord
from app.models.inventory import InventoryMovement, MovementType
from app.models.mock_cod import MockCODConfirmation, MockShipmentReturnReceipt
from app.models.order import OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentItem
from app.schemas.mock_cod import MockSettlementResponse
from app.schemas.order import BackofficeOrderResponse
from app.services import local_sales, sales_access, shipments
from app.services.order_documents import claim_intent, digest, minor, operation_key


def validated_cod_snapshot(order, payment):
    """Fail closed on anything outside the dedicated immutable mock COD link.

    Relationships are selectin loaded; this function is also safe for lightweight
    fixture objects used by the pre-existing fiscal policy tests.
    """
    fact = getattr(payment, 'cod_confirmation', None)
    if not fact or payment.source != 'mock_cod_confirmation' or payment.method != 'cod' or not payment.simulation:
        return None
    shipment = next((s for s in getattr(order, 'shipments', ()) if s.id == fact.shipment_id), None)
    if (not shipment or not shipment.simulation or shipment.carrier != 'mock_nova_poshta'
            or shipment.return_of_shipment_id is not None
            or not shipment.handed_over_at or not any(h.simulation for h in getattr(order, 'handovers', ()))
            or not shipment.tracking_number or not shipment.tracking_number.startswith('MOCK-')
            or fact.tracking_number != shipment.tracking_number or fact.shipment_uuid != shipment.operation_uuid
            or fact.order_id != order.id or fact.payment_id != payment.id or payment.order_id != order.id
            or fact.original_channel != order.channel or fact.original_simulation != order.simulation
            or order.channel not in ('shop', 'backoffice_pos') or order.status == OrderStatus.cancelled
            or fact.amount_minor != payment.amount_minor
            or fact.amount_minor != shipment.packaging.get('cod_amount_minor') or fact.amount_minor <= 0
            or not fact.provider_event_id.startswith('MOCK-') or fact.operator_id != payment.operator_id
            or fact.evidence != payment.evidence or fact.created_at != payment.created_at):
        return None
    return {'confirmation_id': fact.id, 'shipment_id': shipment.id, 'shipment_uuid': fact.shipment_uuid,
        'tracking_number': fact.tracking_number, 'provider_event_id': fact.provider_event_id,
        'amount_minor': fact.amount_minor, 'original_channel': fact.original_channel,
        'original_simulation': fact.original_simulation}


async def validated_cod_provenance(session, order, payment):
    """Database-backed companion for validating persisted fiscal documents."""
    loaded = await local_sales.load(session, order.id)
    current = next((p for p in loaded.payments if p.id == payment.id), None)
    return validated_cod_snapshot(loaded, current) if current else None


async def require(session, actor, *, inventory=False, mutation=True):
    actor = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if not actor or not actor.is_active:
        raise HTTPException(403, 'Active mock carrier operator required')
    await sales_access.deny_master(session, actor)
    permission = 'can_post_inventory' if inventory else 'can_record_sales'
    secondary = getattr(actor, permission, False) if mutation else (actor.can_post_inventory or actor.can_record_sales)
    if not actor.is_superuser and (not actor.can_manage_shipments or not secondary):
        raise HTTPException(403, 'Shipment and inventory permissions required' if inventory else 'Shipment and sales permissions required')
    if mutation and not shipments.available(session):
        shipments.fail('mock_shipments_disabled', 'Explicit local test mock carrier enablement required')
    return actor


async def load_shipment(session, order_id, shipment_id, *, lock=False):
    statement = select(Shipment).where(Shipment.id == shipment_id, Shipment.order_id == order_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(statement)
    if row is None:
        raise HTTPException(404, 'Shipment not found')
    if not row.simulation or row.carrier != 'mock_nova_poshta' or not (row.tracking_number or '').startswith('MOCK-'):
        shipments.fail('mock_shipment_required', 'A reconciled local MOCK TTN is required')
    return row


def parcel_items(order, shipment):
    quantities = {i.order_item_id: i.quantity for i in shipment.shipment_items}
    return [{'order_item_id': i.id, 'product_id': i.product_id, 'name': i.product_name,
        'quantity': quantities.get(i.id, i.quantity)} for i in sorted(order.items, key=lambda i: i.id)
        if not quantities or i.id in quantities]


async def assert_no_parcel_receipt(session, order_id):
    """Fiscal physical receipts must never duplicate this parcel workflow."""
    if await session.scalar(select(MockShipmentReturnReceipt.id).where(MockShipmentReturnReceipt.order_id == order_id).limit(1)):
        shipments.fail('parcel_receipt_exists', 'This order already has a physical mock TTN receipt')


async def assert_no_fiscal_receipt(session, order_id):
    if await session.scalar(select(MockReturnReceipt.id).join(ReturnRecord, ReturnRecord.id == MockReturnReceipt.return_id)
            .where(ReturnRecord.order_id == order_id).limit(1)):
        shipments.fail('fiscal_return_receipt_exists', 'This order already has a physical fiscal return receipt')


async def settlement(session, actor, order_id, shipment_id):
    actor = await require(session, actor, mutation=False)
    order = await local_sales.load(session, order_id)
    shipment = await load_shipment(session, order_id, shipment_id)
    cod = await session.scalar(select(MockCODConfirmation).where(MockCODConfirmation.shipment_id == shipment.id))
    receipt = await session.scalar(select(MockShipmentReturnReceipt).where(MockShipmentReturnReceipt.shipment_id == shipment.id))
    enabled = shipments.available(session)
    sales = actor.is_superuser or actor.can_record_sales
    inventory = actor.is_superuser or actor.can_post_inventory
    actions = []
    amount = shipment.packaging.get('cod_amount_minor', 0)
    handover = shipment.handed_over_at and any(h.simulation for h in order.handovers)
    if enabled and sales and handover and shipment.active and shipment.status == 'delivered' and shipment.return_of_shipment_id is None:
        if not order.payments and amount > 0 and amount == minor(order.total_amount):
            actions.append('confirm_cod')
        if not any(s.return_of_shipment_id == shipment.id for s in order.shipments):
            actions.append('create_return_shipment')
    fiscal_receipt = await session.scalar(select(MockReturnReceipt.id).join(ReturnRecord, ReturnRecord.id == MockReturnReceipt.return_id)
        .where(ReturnRecord.order_id == order_id).limit(1))
    if enabled and inventory and handover and shipment.status == 'returned' and not receipt and not fiscal_receipt:
        actions.append('receive_return')
    return MockSettlementResponse(available=enabled, order_id=order.id, shipment_id=shipment.id,
        tracking_number=shipment.tracking_number, revision=order.revision, amount_minor=amount,
        cod_payment={k: getattr(cod, k) for k in ('id','payment_id','provider_event_id','amount_minor','evidence','operator_id','created_at')} if cod else None,
        return_receipt={k: getattr(receipt, k) for k in ('id','tracking_number','items','evidence','operator_id','created_at')} if receipt else None,
        items=parcel_items(order, shipment), allowed_actions=actions,
        blocking_reasons=[] if enabled else ['mock_shipments_disabled'])


async def begin(session, actor, order_id, shipment_id, payload, key, operation, *, inventory=False):
    actor = await require(session, actor, inventory=inventory)
    intent = await claim_intent(session, operation_key(key, f'mock-{operation}:{actor.id}:{order_id}:{shipment_id}'),
        digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return actor, intent, None, None
    order = await local_sales.load(session, order_id, lock=True)
    shipment = await load_shipment(session, order_id, shipment_id, lock=True)
    if order.revision != payload.expected_revision:
        shipments.fail('revision_conflict', 'Reload the order')
    if order.channel not in ('shop', 'backoffice_pos') or order.status == OrderStatus.cancelled:
        shipments.fail('mock_order_required', 'A current shop or POS order is required')
    from app.services.qr_payment import block_mutation
    await block_mutation(session, order_id)
    if not shipment.handed_over_at or not any(h.simulation for h in order.handovers):
        shipments.fail('handover_required', 'Explicit mock carrier handover is required')
    return actor, intent, order, shipment


async def finish(session, actor, intent, order):
    order.revision += 1
    await session.flush()
    result = await shipments.result(session, order.id, actor)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode='json')
    await session.commit()
    return result


async def confirm_cod(session, actor, order_id, shipment_id, payload, key):
    actor, intent, order, shipment = await begin(session, actor, order_id, shipment_id, payload, key, 'cod')
    if order is None:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    if shipment.return_of_shipment_id is not None or shipment.status != 'delivered' or not shipment.active:
        shipments.fail('delivered_shipment_required', 'COD requires a delivered outbound mock TTN')
    total = minor(order.total_amount)
    if total <= 0 or shipment.packaging.get('cod_amount_minor') != total:
        shipments.fail('cod_amount_mismatch', 'COD must match the exact current full server total')
    # Provider event identity spans orders and idempotency keys. Serialize that
    # identity before the check; the unique constraint remains the durable guard.
    await session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:event, 0))'),
        {'event':'mock-cod:' + payload.provider_event_id})
    if order.payments or await session.scalar(select(MockCODConfirmation.id).where(
            MockCODConfirmation.provider_event_id == payload.provider_event_id)):
        shipments.fail('cod_payment_exists', 'The order, shipment or mock provider event already has a payment')
    now = datetime.now(UTC)
    payment = OrderPayment(order_id=order.id, amount_minor=total, method='cod', source='mock_cod_confirmation',
        evidence=payload.evidence, operator_id=actor.id, simulation=True, created_at=now)
    session.add(payment)
    await session.flush()
    session.add(MockCODConfirmation(order_id=order.id, shipment_id=shipment.id, payment_id=payment.id,
        provider_event_id=payload.provider_event_id, tracking_number=shipment.tracking_number,
        shipment_uuid=shipment.operation_uuid, amount_minor=total, original_channel=order.channel,
        original_simulation=order.simulation, evidence=payload.evidence, operator_id=actor.id, created_at=now))
    shipments.event(session, shipment, actor, 'cod_payment', 'mock_cod_confirmed')
    try:
        return await finish(session, actor, intent, order)
    except IntegrityError as error:
        await session.rollback()
        if getattr(error.orig, 'sqlstate', None) == '23505':
            shipments.fail('cod_payment_exists', 'The order, shipment or mock provider event already has a payment')
        raise


async def create_return_shipment(session, actor, order_id, shipment_id, payload, key):
    actor, intent, order, original = await begin(session, actor, order_id, shipment_id, payload, key, 'return-shipment')
    if order is None:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    if original.status != 'delivered' or original.return_of_shipment_id is not None:
        shipments.fail('delivered_shipment_required', 'A delivered original mock TTN is required')
    if any(s.return_of_shipment_id == original.id for s in order.shipments):
        shipments.fail('return_shipment_exists', 'This original parcel already has a mock return TTN')
    await assert_no_fiscal_receipt(session, order.id)
    await assert_no_parcel_receipt(session, order.id)
    items = parcel_items(order, original)
    if payload.items is not None:
        quantities = {i.order_item_id:i.quantity for i in payload.items}
        capacity = {i['order_item_id']:i['quantity'] for i in items}
        if len(quantities) != len(payload.items) or any(k not in capacity or q > capacity[k] for k,q in quantities.items()):
            shipments.fail('return_capacity_exceeded', 'Return parcel quantities must belong to the original parcel')
        items = [{**i, 'quantity':quantities[i['order_item_id']]} for i in items if i['order_item_id'] in quantities]
    for item in items:
        shipped = await session.scalar(select(func.coalesce(func.sum(-InventoryMovement.on_hand_delta),0)).where(
            InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == item['order_item_id'],
            InventoryMovement.movement_type == MovementType.shipment, InventoryMovement.on_hand_delta < 0))
        restored = await session.scalar(select(func.coalesce(func.sum(InventoryMovement.on_hand_delta),0)).where(
            InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == item['order_item_id'],
            InventoryMovement.movement_type == MovementType.customer_return))
        if item['quantity'] + restored > shipped:
            shipments.fail('return_capacity_exceeded', 'Return parcel exceeds actual shipped quantity minus prior credits')
    tracking, now = 'MOCK-' + uuid4().hex.upper(), datetime.now(UTC)
    original.active = False
    await session.flush()
    returned = Shipment(order_id=order.id, simulation=True, carrier='mock_nova_poshta', source='mock_return',
        active=True, operation_uuid=str(uuid4()), provider_reference=tracking, tracking_number=tracking,
        status='returning', raw_status='returning', progress=4, address_snapshot=original.address_snapshot,
        packaging={**original.packaging, 'cod_amount_minor': 0}, return_of_shipment_id=original.id,
        return_evidence=payload.evidence, return_operator_id=actor.id, handed_over_at=now, created_at=now)
    session.add(returned)
    await session.flush()
    session.add_all([ShipmentItem(shipment_id=returned.id, order_item_id=i['order_item_id'], quantity=i['quantity'])
        for i in items])
    shipments.event(session, returned, actor, 'return_shipment', 'returning')
    return await finish(session, actor, intent, order)


async def receive_return(session, actor, order_id, shipment_id, payload, key):
    actor, intent, order, shipment = await begin(session, actor, order_id, shipment_id, payload, key, 'parcel-receipt', inventory=True)
    if order is None:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    if shipment.status != 'returned':
        shipments.fail('returned_shipment_required', 'Physical receipt requires a returned mock TTN')
    await assert_no_fiscal_receipt(session, order.id)
    await assert_no_parcel_receipt(session, order.id)
    items = parcel_items(order, shipment)
    requested = {i.order_item_id: i for i in payload.items}
    if len(requested) != len(payload.items) or {k:i.quantity for k,i in requested.items()} != {i['order_item_id']:i['quantity'] for i in items}:
        shipments.fail('receipt_quantity_mismatch', 'Explicitly cover every parcel line once with its full quantity')
    restocked = [i for i in items if requested[i['order_item_id']].disposition == 'restock']
    products = {p.id:p for p in (await session.scalars(select(Product).where(Product.id.in_(sorted({i['product_id'] for i in restocked})))
        .order_by(Product.id).with_for_update().execution_options(populate_existing=True))).all()}
    # All physical dispositions consume parcel capacity, including damaged goods.
    for item in items:
        shipped = await session.scalar(select(func.coalesce(func.sum(-InventoryMovement.on_hand_delta),0)).where(
            InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == item['order_item_id'],
            InventoryMovement.movement_type == MovementType.shipment, InventoryMovement.on_hand_delta < 0))
        restored = await session.scalar(select(func.coalesce(func.sum(InventoryMovement.on_hand_delta),0)).where(
            InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == item['order_item_id'],
            InventoryMovement.movement_type == MovementType.customer_return))
        if item['quantity'] + restored > shipped:
            shipments.fail('shipped_quantity_exceeded', 'Physical receipt cannot exceed original shipped quantity minus previous credits')
    receipt = MockShipmentReturnReceipt(order_id=order.id, shipment_id=shipment.id,
        tracking_number=shipment.tracking_number, items=[i.model_dump() for i in sorted(payload.items,key=lambda i:i.order_item_id)],
        operator_id=actor.id, evidence=payload.evidence, created_at=datetime.now(UTC))
    session.add(receipt)
    await session.flush()
    for item in restocked:
        if item['product_id'] not in products:
            shipments.fail('product_missing', 'Original parcel product is missing')
        products[item['product_id']].stock_quantity += item['quantity']
        session.add(InventoryMovement(product_id=item['product_id'], movement_type=MovementType.customer_return,
            quantity=item['quantity'], on_hand_delta=item['quantity'], reserved_delta=0, order_id=order.id,
            order_item_id=item['order_item_id'], performed_by_user_id=actor.id,
            reference_key=f'mock-parcel-return:{receipt.id}:{item["order_item_id"]}', reason=payload.evidence))
    shipments.event(session, shipment, actor, 'return_receipt', 'mock_parcel_received')
    return await finish(session, actor, intent, order)
