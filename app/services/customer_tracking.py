"""Safe owner projections and bounded refresh scheduling. No carrier call on GET."""
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.models.order import Order
from app.models.shipment import Shipment, ShipmentItem
from app.schemas.shipment import CustomerShipmentResponse, TrackingEventResponse


def aware(value):
    return value.replace(tzinfo=value.tzinfo or UTC) if value else None


def project(shipment, *, detail=False):
    real = not shipment.simulation and shipment.carrier == 'nova_poshta'
    checked = aware(shipment.last_checked_at)
    status = (shipment.tracking_status or 'requires_review') if real else shipment.status
    return CustomerShipmentResponse(
        id=shipment.id, tracking_number=shipment.tracking_number, status=shipment.status,
        simulation=shipment.simulation, carrier=shipment.carrier, last_checked_at=checked,
        delivery_status=status, provider_status_code=shipment.raw_status if real and checked else None,
        provider_event_at=shipment.provider_event_at if real else None,
        last_successful_check_at=checked,
        stale=(real and shipment.last_attempt_status != 'success') or not checked or checked < datetime.now(UTC) - timedelta(seconds=settings.np_tracking_stale_seconds),
        last_attempt_status=('error' if shipment.last_attempt_status not in ('never', 'success') else shipment.last_attempt_status) if real else 'never',
        return_of_shipment_id=shipment.return_of_shipment_id,
        shop_carrier_return_case_id=getattr(shipment, 'shop_carrier_return_case_id', None),
        provenance=getattr(shipment, 'provenance', None), authority=getattr(shipment, 'authority', None),
        items=list(shipment.shipment_items) or None,
        events=[TrackingEventResponse.model_validate(e) for e in shipment.tracking_events] if detail else [],
    )


async def queue_refresh(session, order_id, customer_id):
    # Lock ownership and shipments, serialize requests across every API process.
    order = await session.scalar(select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
                                 .with_for_update().execution_options(populate_existing=True))
    if order is None:
        raise HTTPException(404, 'Order not found')
    if not settings.np_tracking_enabled or not settings.nova_poshta_api_key:
        return {'status': 'disabled', 'retry_after_seconds': 0}
    rows = list((await session.scalars(select(Shipment).where(Shipment.order_id == order.id,
        Shipment.simulation.is_(False), Shipment.carrier == 'nova_poshta', Shipment.active.is_(True))
        .with_for_update().execution_options(populate_existing=True))).all())
    if not rows:
        return {'status': 'no_real_shipments', 'retry_after_seconds': 0}
    now = datetime.now(UTC)
    eligible = []
    waits = []
    for row in rows:
        # Reuse next_poll_at as a durable cooldown. Never override provider backoff.
        due = aware(row.next_poll_at)
        if due and due > now:
            waits.append(ceil((due - now).total_seconds()))
        else:
            eligible.append(row)
    if not eligible:
        return {'status': 'cooldown', 'retry_after_seconds': min(waits)}
    # Due shipments are already in the scheduler queue. Do not bypass its locks,
    # global request throttle or backoff and do not call the provider from HTTP.
    return {'status': 'queued', 'retry_after_seconds': settings.np_tracking_poll_seconds}


async def attach_existing(session, actor, order_id, payload):
    from app.services.shipments import allowed, carrier_order
    from app.services.local_sales import load
    if not allowed(actor):
        raise HTTPException(403, 'Shipment permission required')
    order = await load(session, order_id, lock=True)
    if order.revision != payload.expected_revision:
        raise HTTPException(409, 'Reload order')
    if not carrier_order(order):
        raise HTTPException(409, 'Nova Poshta fulfillment required')
    if order.simulation or any(s.simulation for s in order.shipments):
        raise HTTPException(409, 'Real and mock shipments cannot be mixed')
    if await session.scalar(select(Shipment.id).where(Shipment.tracking_number == payload.tracking_number)):
        raise HTTPException(409, 'Tracking number already registered')
    parent = None
    if payload.return_of_shipment_id:
        parent = next((s for s in order.shipments if s.id == payload.return_of_shipment_id
                       and not s.simulation and s.carrier == 'nova_poshta' and not s.return_of_shipment_id), None)
        if parent is None:
            raise HTTPException(422, 'Return must reference an outbound shipment of this order')
    items = {i.id: i for i in order.items}
    seen = set()
    for allocation in payload.items:
        if allocation.order_item_id not in items or allocation.quantity < 1 or allocation.order_item_id in seen:
            raise HTTPException(422, 'Invalid shipment items')
        seen.add(allocation.order_item_id)
        used = sum(a.quantity for s in order.shipments if s.active and (s.tracking_status if not s.simulation else s.status) != 'cancelled'
                   and (s.return_of_shipment_id == parent.id if parent else not s.return_of_shipment_id)
                   for a in s.shipment_items if a.order_item_id == allocation.order_item_id)
        capacity = (next((a.quantity for a in parent.shipment_items if a.order_item_id == allocation.order_item_id), 0)
                    if parent else items[allocation.order_item_id].quantity)
        if allocation.quantity + used > capacity:
            raise HTTPException(422, 'Shipment quantity exceeds order quantity')
    now = datetime.now(UTC)
    shipment = Shipment(order_id=order.id, simulation=False, carrier='nova_poshta', source='attach', active=True,
        operation_uuid=str(uuid4()), tracking_number=payload.tracking_number, provider_reference=payload.tracking_number,
        status='requires_review', raw_status='', tracking_status=None, progress=0,
        address_snapshot={}, packaging={}, created_at=now, return_of_shipment_id=payload.return_of_shipment_id)
    session.add(shipment)
    await session.flush()
    for allocation in payload.items:
        session.add(ShipmentItem(shipment_id=shipment.id, **allocation.model_dump()))
    order.revision += 1
    await session.commit()
    return {'id': shipment.id, 'tracking_number': shipment.tracking_number, 'simulation': False}
