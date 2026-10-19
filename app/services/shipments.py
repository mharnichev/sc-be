"""Local, database-backed MOCK carrier. There is intentionally no live transport.

Carrier acceptance and journal commit in one transaction because this simulator
shares the DB. This does not claim to model distributed provider atomicity.
"""
from copy import deepcopy
from datetime import UTC, datetime
from html import escape
from uuid import uuid4

from fastapi import HTTPException
from jose import JWTError
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_token
from app.models.order import Order, OrderHandover, OrderStatus
from app.models.shipment import Shipment, ShipmentEvent, MockCarrierRecord
from app.schemas.order import BackofficeOrderResponse
from app.schemas.shipment import ShipmentCommand, PublicShipmentResponse
from app.services import local_sales, sales_access
from app.services.order import OrderService
from app.services.order_documents import claim_intent, digest, operation_key, minor

STATUSES = {'created': ('created', 0), 'handed_over': ('handed_over', 1), 'in_transit': ('in_transit', 2),
            'delivered': ('delivered', 3), 'refused': ('refused', 3),
            'returning': ('returning', 4), 'returned': ('returned', 5)}
TERMINAL = {'delivered', 'returned', 'cancelled'}


def allowed(actor, inventory=False):
    return bool(actor.is_superuser or (getattr(actor, 'can_manage_shipments', False)
                and (not inventory or actor.can_post_inventory)))


def available(session):
    url = session.bind.url
    return bool(settings.np_mock_shipments_enabled and settings.app_env in ('local', 'test')
                and url.get_backend_name() == 'postgresql' and url.host in ('localhost', '127.0.0.1', '::1')
                and 'test' in (url.database or '').lower())


async def require(session, actor, inventory=False, *, mutation=True):
    await sales_access.deny_master(session, actor)
    if not allowed(actor, inventory):
        raise HTTPException(403, 'Shipment and inventory permission required' if inventory else 'Shipment permission required')
    if mutation and not available(session):
        fail('mock_shipments_disabled', 'Only an explicitly enabled isolated local test database is supported')


def fail(code, message):
    raise HTTPException(409, detail={'code': code, 'message': message})


def carrier_order(order):
    return order.fulfillment_method == 'nova_poshta' or (order.fulfillment_method is None
        and order.shipping_company in ('nova_poshta', 'novaPoshta', 'novaPost'))


def actions(order, session, actor):
    enabled = available(session) and allowed(actor)
    active = next((s for s in order.shipments if s.active), None)
    return {'mode': 'mock_only', 'can_manage': enabled, 'simulation_available': available(session),
            'can_create': enabled and carrier_order(order) and active is None and not order.handovers and order.status not in (OrderStatus.cancelled, OrderStatus.completed),
            'can_handover': enabled and allowed(actor, True) and active is not None and active.status == 'created'
                and not order.handovers and order.status not in (OrderStatus.cancelled, OrderStatus.completed),
            'blocking_reasons': [] if enabled else ['mock_shipments_disabled_or_forbidden']}


def address(order):
    snapshot = deepcopy(order.shipping_payload_json or {})
    if not snapshot.get('shippingCityRef') or not (snapshot.get('shippingWarehouseRef') or snapshot.get('shippingStreetRef')):
        fail('address_refs_required', 'Stable NP city and warehouse/street refs required; legacy text is never guessed')
    if snapshot.get('shippingStreetRef') and not snapshot.get('shippingWarehouseRef') and not snapshot.get('buildingNumber'):
        fail('address_incomplete', 'Building number required for street delivery')
    return snapshot


def event(session, shipment, actor, op, raw, outcome='accepted'):
    session.add(ShipmentEvent(shipment_id=shipment.id, operation=op, operation_uuid=str(uuid4()),
        actor_id=actor.id, raw_status=raw, normalized_status=shipment.status, outcome=outcome,
        created_at=datetime.now(UTC)))


async def result(session, order_id, actor):
    order = await local_sales.load(session, order_id)
    response = await local_sales.response(order, session, actor)
    return response


async def fixture(session, actor, order_id, payload, raw_key):
    await require(session, actor, mutation=False)
    intent = await claim_intent(session, operation_key(raw_key, f'np-fixture:{actor.id}:{order_id}'), digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return intent.response_json
    await require(session, actor)
    order = await local_sales.load(session, order_id, lock=True)
    from app.services.qr_payment import block_mutation
    await block_mutation(session, order_id)
    if order.revision != payload.expected_revision:
        fail('revision_conflict', 'Reload order')
    record = MockCarrierRecord(order_id=order_id, tracking_number='MOCK-' + uuid4().hex.upper(),
        address_snapshot=payload.address_snapshot.model_dump(exclude_none=True) if payload.address_snapshot else address(order), created_at=datetime.now(UTC))
    session.add(record)
    intent.order_id = order_id
    intent.response_json = {'tracking_number': record.tracking_number, 'simulation': True, 'revision': order.revision}
    await session.commit()
    return intent.response_json


async def create(session, actor, order_id, payload, raw_key):
    await require(session, actor, mutation=False)
    intent = await claim_intent(session, operation_key(raw_key, f'np-create:{actor.id}:{order_id}'), digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    await require(session, actor)
    order = await local_sales.load(session, order_id, lock=True)
    from app.services.qr_payment import block_mutation
    await block_mutation(session, order_id)
    if order.revision != payload.expected_revision:
        fail('revision_conflict', 'Reload order')
    if order.status in (OrderStatus.cancelled, OrderStatus.completed) or order.handovers:
        fail('terminal_order', 'Closed or handed-over orders cannot create shipments')
    if any(not s.simulation for s in order.shipments):
        fail('real_shipments_exist', 'Real and mock shipments cannot be mixed')
    if any(s.active for s in order.shipments):
        fail('active_shipment_exists', 'Reconcile or cancel the existing shipment first')
    if not carrier_order(order):
        fail('carrier_fulfillment_required', 'Only Nova Poshta delivery orders support shipments')
    snapshot = payload.address_snapshot.model_dump(exclude_none=True) if payload.address_snapshot else address(order)
    if payload.packaging.cod_amount_minor not in (0, minor(order.total_amount)):
        fail('cod_amount_mismatch', 'COD must be zero or the exact server order total')
    if payload.packaging.cod_amount_minor and order.payments:
        fail('payment_exists', 'COD cannot collect an already recorded payment')
    operation_uuid = str(uuid4())
    tracking = 'MOCK-' + uuid4().hex.upper()
    if payload.source == 'attach':
        record = (await session.execute(select(MockCarrierRecord).where(
            MockCarrierRecord.tracking_number == payload.tracking_number,
            MockCarrierRecord.order_id == order_id).with_for_update())).scalar_one_or_none()
        if record is None or record.address_snapshot != snapshot:
            fail('mock_tracking_not_verified', 'Use an unused mock fixture belonging to this order and exact address')
        if await session.scalar(select(Shipment.id).where(Shipment.tracking_number == payload.tracking_number)):
            fail('tracking_already_attached', 'Tracking was already attached')
        tracking = record.tracking_number
    timeout = payload.mock_outcome == 'timeout_after_acceptance'
    shipment = Shipment(order_id=order_id, simulation=True, carrier='mock_nova_poshta', source=payload.source,
        active=True, operation_uuid=operation_uuid, provider_reference=tracking,
        tracking_number=None if timeout else tracking, status='unknown' if timeout else 'created',
        raw_status='timeout_after_acceptance' if timeout else 'created', progress=0,
        address_snapshot=snapshot, packaging=payload.packaging.model_dump(), created_at=datetime.now(UTC))
    session.add(shipment)
    await session.flush()
    event(session, shipment, actor, payload.source, shipment.raw_status, 'unknown' if timeout else 'accepted')
    order.revision += 1
    await session.flush()
    response = await result(session, order_id, actor)
    intent.order_id, intent.response_json = order_id, response.model_dump(mode='json')
    await session.commit()
    return response


async def command(session, actor, order_id, shipment_id, payload, raw_key, op):
    await require(session, actor, inventory=op == 'handover', mutation=False)
    if op != 'refresh' and payload.mock_status is not None:
        raise HTTPException(422, 'mock_status is only valid for refresh')
    if op == 'handover' and not payload.evidence:
        raise HTTPException(422, 'Handover evidence is required')
    intent = await claim_intent(session, operation_key(raw_key, f'np-{op}:{actor.id}:{order_id}:{shipment_id}'), digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return BackofficeOrderResponse.model_validate(intent.response_json)
    await require(session, actor, inventory=op == 'handover')
    order = await local_sales.load(session, order_id, lock=True)
    from app.services.qr_payment import block_mutation
    await block_mutation(session, order_id)
    shipment = (await session.execute(select(Shipment).where(Shipment.id == shipment_id, Shipment.order_id == order_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if shipment is None:
        raise HTTPException(404, 'Shipment not found')
    if not shipment.simulation:
        fail('live_unavailable', 'Live adapter is not implemented')
    if order.revision != payload.expected_revision:
        fail('revision_conflict', 'Reload order')
    raw, outcome = shipment.raw_status, 'accepted'
    if op == 'refresh':
        shipment.last_checked_at = datetime.now(UTC)
        # Reconciliation looks up the persisted external reference for the SAME
        # UUID. Never creates a replacement shipment after an unknown outcome.
        if shipment.status == 'unknown':
            shipment.tracking_number = shipment.provider_reference
        raw = payload.mock_status or ('created' if shipment.status == 'unknown' else shipment.raw_status)
        normalized, progress = STATUSES.get(raw, ('requires_review', shipment.progress))
        if shipment.status in TERMINAL:
            outcome = 'ignored_terminal'
        elif normalized == 'requires_review':
            shipment.status = normalized
        elif progress < shipment.progress:
            outcome = 'ignored_stale'
        elif progress >= 2 and shipment.handed_over_at is None:
            shipment.status = 'requires_review'
            outcome = 'handover_missing'
        elif shipment.progress >= 3 and progress == 3 and normalized != shipment.status:
            shipment.status = 'requires_review'
            outcome = 'conflicting_terminal'
        else:
            shipment.status, shipment.progress = normalized, progress
        shipment.raw_status = raw
    elif op == 'cancel':
        if shipment.handed_over_at or order.handovers:
            fail('already_handed_over', 'Carrier handover cannot be cancelled; use return reconciliation')
        if shipment.status != 'created':
            fail('reconcile_required', 'Only a confirmed created shipment can be cancelled')
        shipment.status, shipment.raw_status, shipment.active = 'cancelled', 'cancelled', False
        raw = 'cancelled'
    elif op == 'handover':
        if shipment.status != 'created' or shipment.handed_over_at or order.handovers:
            fail('handover_unavailable', 'A confirmed created shipment without prior handover is required')
        # Reuse stage-2 inventory ledger/ownership/lock policy. COD is not a
        # payment fact and does not require or create a fabricated payment.
        await OrderService().update_status(session, order_id, OrderStatus.completed,
            admin_user_id=actor.id, profile_handover=True, commit=False)
        shipment.handed_over_at = datetime.now(UTC)
        shipment.status, shipment.raw_status, shipment.progress = 'handed_over', 'handed_over', 1
        raw = 'handed_over'
        session.add(OrderHandover(order_id=order_id, operator_id=actor.id,
            evidence=payload.evidence, simulation=True, created_at=shipment.handed_over_at))
    elif op == 'label':
        if not shipment.tracking_number or shipment.status in ('unknown', 'cancelled'):
            fail('label_unavailable', 'Reconcile the shipment before printing')
    else:
        raise HTTPException(422, 'Unknown command')
    event(session, shipment, actor, op, raw, outcome)
    if op != 'handover':
        order.revision += 1
    await session.flush()
    response = await result(session, order_id, actor)
    intent.order_id, intent.response_json = order_id, response.model_dump(mode='json')
    await session.commit()
    return response


async def poll_mock_shipments(session, actor, *, limit=20):
    """Bounded explicit polling; only the mock adapter and isolated test DB.

    Per-order locks/revision checks serialize competing pollers; no cron/live
    transport is installed. Operators may run this endpoint or refresh directly.
    """
    await require(session, actor)
    ids = list((await session.execute(select(Shipment.id, Shipment.order_id).where(
        Shipment.simulation.is_(True), Shipment.carrier == 'mock_nova_poshta',
        Shipment.active.is_(True), Shipment.status.notin_(TERMINAL)).order_by(
        Shipment.last_checked_at.asc().nullsfirst(), Shipment.id).limit(min(max(limit, 1), 100)))).all())
    results = []
    for shipment_id, order_id in ids:
        order = await local_sales.load(session, order_id)
        try:
            await command(session, actor, order_id, shipment_id,
                ShipmentCommand(expected_revision=order.revision), str(uuid4()), 'refresh')
            results.append(shipment_id)
        except HTTPException as error:
            await session.rollback()
            if error.status_code != 409:
                raise
    return {'polled': results, 'mode': 'mock_only'}


async def guest_tracking(session, token):
    try:
        claims = decode_token(token)
        if claims.get('scope') != 'guest_order_tracking':
            raise ValueError()
        order_id = int(claims['sub'])
    except (JWTError, ValueError, TypeError, KeyError):
        raise HTTPException(404, 'Tracking not found') from None
    order = await session.get(Order, order_id)
    if not order or order.customer_id is not None or order.guest_tracking_revoked_at or not order.guest_tracking_expires_at:
        raise HTTPException(404, 'Tracking not found')
    expiry = order.guest_tracking_expires_at
    if expiry.replace(tzinfo=expiry.tzinfo or UTC) <= datetime.now(UTC):
        raise HTTPException(404, 'Tracking not found')
    return {'order_id': order.id, 'shipments': [PublicShipmentResponse.model_validate(s) for s in order.shipments]}


def label_html(shipment):
    # Generated SC mock label, never provider HTML/URL/QR or a real carrier label.
    return ('<!doctype html><html lang="uk"><meta charset="utf-8"><title>Тестова НП</title>'
            '<h1>Тестова НП — НЕ ДЛЯ ВІДПРАВЛЕННЯ</h1><p>Макет етикетки SC. Симуляція.</p><p>'
            + escape(shipment.tracking_number or '') + '</p><p>Місць: '
            + escape(str(shipment.packaging['places'])) + '</p></html>')
