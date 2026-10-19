"""Opt-in isolated PostgreSQL concurrency tests; provider is always a fixture."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.order import Order, OrderStatus
from app.models.shipment import Shipment, TrackingEvent, TrackingPollState
from app.services import np_tracking as tracking
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401

NUMBER = '20450000000001'


async def seed(database):
    async with database() as session:
        order = Order(customer_name='Fixture', customer_phone='+380501112233',
            total_amount=Decimal('100.00'), status=OrderStatus.completed)
        session.add(order)
        await session.flush()
        real = Shipment(order_id=order.id, simulation=False, carrier='nova_poshta', source='attach',
            operation_uuid=str(uuid4()), tracking_number=NUMBER, provider_reference=NUMBER,
            status='created', raw_status='1', address_snapshot={}, packaging={},
            created_at=datetime.now(UTC), active=True)
        mock = Shipment(order_id=order.id, simulation=True, carrier='mock_nova_poshta', source='attach',
            operation_uuid=str(uuid4()), tracking_number='MOCK-FIXTURE', provider_reference='MOCK-FIXTURE',
            status='created', raw_status='created', address_snapshot={}, packaging={},
            created_at=datetime.now(UTC), active=True)
        session.add_all([real, mock])
        await session.commit()
        return order.id, real.id, mock.id


@pytest.mark.anyio
async def test_concurrent_poll_deduplicates_and_durable_spacing(database, monkeypatch):
    monkeypatch.setattr(tracking.settings, 'np_tracking_enabled', True)
    monkeypatch.setattr(tracking.settings, 'nova_poshta_api_key', 'FIXTURE-SECRET')
    monkeypatch.setattr(tracking.settings, 'np_tracking_request_interval_seconds', 60)
    order_id, real_id, mock_id = await seed(database)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def provider(self, numbers):
        calls.append(numbers)
        started.set()
        await release.wait()
        return {NUMBER: tracking.Observation(NUMBER, '9', datetime(2026, 10, 17, tzinfo=UTC))}
    monkeypatch.setattr(tracking.NovaPoshtaTracking, 'fetch', provider)
    async def attempt():
        async with database() as session:
            count = await tracking.refresh_due(session)
            await session.commit()
            return count
    first = asyncio.create_task(attempt())
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        assert await asyncio.wait_for(attempt(), timeout=5) == 0
    finally:
        release.set()
    assert await first == 1
    async with database() as session:
        real = await session.get(Shipment, real_id)
        real.next_poll_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await attempt() == 0  # Persistent spacing, even when a shipment is due.
    assert calls == [[NUMBER]]
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(TrackingEvent)) == 1
        real = await session.get(Shipment, real_id)
        mock = await session.get(Shipment, mock_id)
        order = await session.get(Order, order_id)
        assert real.tracking_status == 'delivered'
        assert mock.tracking_status is None and mock.last_checked_at is None
        assert order.status == OrderStatus.completed
        assert not order.payments  # Receipt cannot manufacture payment.


@pytest.mark.anyio
async def test_error_backoff_preserves_status_then_retry_is_idempotent(database, monkeypatch):
    monkeypatch.setattr(tracking.settings, 'np_tracking_enabled', True)
    monkeypatch.setattr(tracking.settings, 'nova_poshta_api_key', 'FIXTURE-SECRET')
    _, real_id, _ = await seed(database)
    when = datetime(2026, 10, 17, tzinfo=UTC)
    async with database() as session:
        real = await session.get(Shipment, real_id, with_for_update=True)
        await tracking.apply_observation(session, real, tracking.Observation(NUMBER, '7', when), when)
        real.next_poll_at = when - timedelta(days=1)
        await session.commit()
    async def failed(self, numbers):
        raise tracking.TrackingError('timeout')
    monkeypatch.setattr(tracking.NovaPoshtaTracking, 'fetch', failed)
    async with database() as session:
        assert await tracking.refresh_due(session) == 1
        await session.commit()
    async with database() as session:
        real = await session.get(Shipment, real_id)
        assert real.tracking_status == 'ready_for_pickup'
        assert real.last_checked_at == when
        assert real.last_attempt_status == 'timeout'
        assert real.next_poll_at > datetime.now(UTC)
    # Competing row-locked observations serialize and preserve event uniqueness.
    async def repeat():
        async with database() as session:
            real = await session.scalar(select(Shipment).where(Shipment.id == real_id).with_for_update())
            await tracking.apply_observation(session, real, tracking.Observation(NUMBER, '7', when), datetime.now(UTC))
            await session.commit()
    await asyncio.gather(repeat(), repeat())
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(TrackingEvent)) == 1
        assert (await session.get(TrackingPollState, 1)).next_request_at is not None
