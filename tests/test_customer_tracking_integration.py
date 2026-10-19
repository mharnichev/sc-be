"""Synthetic local PostgreSQL order history, owner access and allocations."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.orm import selectinload
from app.models.product import Product

from app.api.v1.routes import orders
from app.models.admin_user import AdminUser
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.shipment import Shipment
from app.schemas.shipment import RealShipmentAttach
from app.services import customer_tracking as tracking
from app.services.np_tracking import Observation, apply_observation
from tests.test_shipments_integration import setup_order
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies  # noqa: F401

pytestmark = pytest.mark.anyio


async def attach(database, oid, number, items=(), parent=None):
    async with database() as session:
        order = await session.scalar(select(Order).where(Order.id == oid).options(selectinload(Order.items)))
        return await tracking.attach_existing(session, await session.get(AdminUser, 1), oid,
            RealShipmentAttach(expected_revision=order.revision, tracking_number=number,
                items=items, return_of_shipment_id=parent))


async def test_partial_history_returns_owner_scope_and_cancel_replacement(database, checkout_dependencies, monkeypatch):
    monkeypatch.setattr(tracking.settings, 'np_tracking_enabled', True)
    oid, _ = await setup_order(database, checkout_dependencies)
    async with database() as session:
        customer = Customer(phone='+380501112234')
        session.add(customer)
        await session.flush()
        order = await session.scalar(select(Order).where(Order.id == oid).options(selectinload(Order.items)))
        order.customer_id = customer.id
        item_id = order.items[0].id
        customer_id = customer.id
        await session.commit()
    allocation = [{'order_item_id': item_id, 'quantity': 1}]
    first = await attach(database, oid, '20450000000001', allocation)
    async with database() as session:
        row = await session.get(Shipment, first['id'], with_for_update=True)
        await apply_observation(session, row, Observation(row.tracking_number, '2', datetime.now(UTC)), datetime.now(UTC))
        await session.commit()
    # A cancelled real TTN frees the allocation for its replacement.
    second = await attach(database, oid, '20450000000002', allocation)
    third = await attach(database, oid, '20450000000003')  # Unknown contents stay unknown.
    now = datetime.now(UTC)
    async with database() as session:
        row = await session.get(Shipment, second['id'], with_for_update=True)
        await apply_observation(session, row, Observation(row.tracking_number, '9', now), now)
        order = await session.scalar(select(Order).where(Order.id == oid).options(selectinload(Order.items)))
        order.status = OrderStatus.completed
        await session.commit()
    returned = await attach(database, oid, '20450000000004', allocation, second['id'])
    async with database() as session:
        row = await session.get(Shipment, returned['id'], with_for_update=True)
        await apply_observation(session, row, Observation(row.tracking_number, '9', now + timedelta(hours=1)), now)
        await session.commit()
    async with database() as session:
        customer = SimpleNamespace(id=customer_id)
        detail = await orders.get_my_order(oid, customer, session)
        assert len(detail.shipments) == 4 and detail.payment_status != 'paid'
        assert detail.delivery_summary == {'cancelled': 1, 'delivered': 1, 'requires_review': 1, 'returned': 1}
        assert detail.shipments[1].items[0].quantity == 1
        assert detail.shipments[2].items is None
        assert detail.shipments[3].return_of_shipment_id == second['id']
        assert detail.shipments[3].events[0].normalized_status == 'returned'
        assert detail.items[0].product_name and detail.items[0].price == 100
        history = await orders.list_my_orders(1, 20, customer, session)
        assert history.total == 1 and history.items[0].status == OrderStatus.completed
        assert history.items[0].shipments[1].events == []
        with pytest.raises(HTTPException) as denied:
            await orders.get_my_order(oid, SimpleNamespace(id=customer_id + 1), session)
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as denied:
            await tracking.queue_refresh(session, oid, customer_id + 1)
        assert denied.value.status_code == 404
    async with database() as session:
        order = await session.scalar(select(Order).where(Order.id == oid).options(selectinload(Order.items)))
        assert not order.payments


async def test_history_query_count_constant_for_multiple_orders(database, checkout_dependencies):
    first, pid = await setup_order(database, checkout_dependencies)
    async with database() as session:
        await session.execute(update(Product).where(Product.id == pid).values(slug='first-product'))
        await session.commit()
    second, _ = await setup_order(database, checkout_dependencies)
    async with database() as session:
        customer = Customer(phone='+380501112235')
        session.add(customer)
        await session.flush()
        for oid in (first, second):
            (await session.get(Order, oid)).customer_id = customer.id
        cid = customer.id
        await session.commit()
    await attach(database, first, '20450000000001')
    await attach(database, second, '20450000000002')
    counts = []
    for size in (1, 20):
        async with database() as session:
            statements = []
            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)
            engine = session.bind.sync_engine
            event.listen(engine, 'before_cursor_execute', record)
            try:
                result = await orders.list_my_orders(1, size, SimpleNamespace(id=cid), session)
                assert len(result.items) == min(size, 2)
            finally:
                event.remove(engine, 'before_cursor_execute', record)
            counts.append(len(statements))
    assert counts[0] == counts[1], counts
