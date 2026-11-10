"""Admin cancellation through migrated PostgreSQL and the fake Mono transport."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes.shop_online_payment import backoffice_router
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_online_payment import OnlinePaymentCancellationCommand
from app.services import shop_online_cancellation as cancel, shop_online_payment as payment
from app.services.monobank_provider import MonobankError
from app.services.order import OrderService
from tests.test_shop_fiscal_integration import anyio_backend, checkout_dependencies, database, template_database
from tests.test_shop_online_payment_integration import online, prepared, start
from tests.test_shop_payment_resume_integration import create_order

pytestmark = pytest.mark.anyio
ADMIN = SimpleNamespace(id=1)


async def cancellation(database, order_id, revision=None, key=None):
    async with database() as session:
        if revision is None:
            return await cancel.read(session, ADMIN, order_id)
        return await cancel.cancel(session, ADMIN, order_id,
            OnlinePaymentCancellationCommand(expected_revision=revision), key or str(uuid4()))


async def stock(database, product_id, order_id):
    async with database() as session:
        product = await session.get(Product, product_id)
        order = await session.get(Order, order_id)
        releases = await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.order_id == order_id,
            InventoryMovement.movement_type == InventoryMovementType.reservation_release))
        return product.stock_quantity, product.reserved_quantity, order.status.value, releases


async def test_untouched_cancellation_checks_revision_and_releases_only_its_reserve(
    database, checkout_dependencies, online,
):
    order_id, product_id, _ = await prepared(database)
    other, _ = await create_order(database, product_id)
    state = await cancellation(database, order_id)
    assert state.status == "available" and state.reason == "payment_not_started"
    async with database() as session:
        with pytest.raises(HTTPException) as legacy:
            await OrderService().update_status(session, order_id, OrderStatus.cancelled, admin_user_id=1)
        assert legacy.value.detail["code"] == "online_cancel_command_required"
    with pytest.raises(HTTPException) as stale:
        await cancellation(database, order_id, state.revision + 1)
    assert stale.value.detail["code"] == "revision_conflict"

    key = str(uuid4())
    results = await asyncio.gather(*(cancellation(database, order_id, state.revision, key) for _ in range(2)))
    assert results[0] == results[1] and results[0].status == "cancelled"
    with pytest.raises(HTTPException) as conflicting_key:
        await cancellation(database, order_id, state.revision + 1, key)
    assert conflicting_key.value.detail["code"] == "idempotency_conflict"
    assert await stock(database, product_id, order_id) == (4, 1, "cancelled", 1)
    assert await stock(database, product_id, other) == (4, 1, "pending", 0)
    assert online["calls"] == []


@pytest.mark.parametrize("after_fence", [False, True])
async def test_cancel_and_dispatch_are_serialized_across_sessions(
    database, checkout_dependencies, online, monkeypatch, after_fence,
):
    order_id, product_id, token = await prepared(database)
    provider = payment.gateway()
    create_invoice = provider.create_invoice
    paused, resume = asyncio.Event(), asyncio.Event()

    async def hold():
        paused.set()
        await resume.wait()

    async def delayed_create(*args, before_dispatch, **kwargs):
        if not after_fence:
            await hold()

        async def pause_after_claim():
            await before_dispatch()
            if after_fence:
                await hold()

        return await create_invoice(*args, before_dispatch=pause_after_claim, **kwargs)

    monkeypatch.setattr(provider, "create_invoice", delayed_create)
    monkeypatch.setattr(payment, "gateway", lambda: provider)
    running = asyncio.create_task(start(database, order_id, token))
    try:
        await asyncio.wait_for(paused.wait(), timeout=10)
        state = await cancellation(database, order_id)
        if after_fence:
            assert state.reason == "invoice_dispatch_unconfirmed"
            with pytest.raises(HTTPException) as blocked:
                await cancellation(database, order_id, state.revision)
            assert blocked.value.detail["code"] == "invoice_dispatch_unconfirmed"
            assert await stock(database, product_id, order_id) == (4, 1, "pending", 0)
        else:
            assert state.status == "available" and state.reason == "invoice_not_dispatched"
            async with database() as session:
                with pytest.raises(DBAPIError, match="explicit financial reconciliation"):
                    await session.execute(text("UPDATE orders SET status='cancelled' WHERE id=:id"), {"id": order_id})
                await session.rollback()
            assert (await cancellation(database, order_id, state.revision)).status == "cancelled"
            async with database() as session:
                with pytest.raises(DBAPIError, match="Cannot bind an invoice after undispatched Shop cancellation"):
                    await session.execute(text("UPDATE shop_payment_intents SET invoice_id='late-invoice' WHERE order_id=:id"),
                        {"id": order_id})
                await session.rollback()
    finally:
        resume.set()
        result = await running
    assert online["posts"] == int(after_fence)
    assert result.reason == ("payment_pending" if after_fence else "order_closed")
    if after_fence:
        assert (await cancellation(database, order_id)).reason == "invoice_reconciliation_required"
    else:
        assert await stock(database, product_id, order_id) == (4, 0, "cancelled", 1)


@pytest.mark.parametrize("after_fence", [False, True])
async def test_interrupted_worker_leaves_durable_cancellation_decision(
    database, checkout_dependencies, online, monkeypatch, after_fence,
):
    order_id, product_id, token = await prepared(database)
    provider = payment.gateway()
    parked = asyncio.Event()

    async def interrupted_create(*args, before_dispatch, **kwargs):
        if after_fence:
            await before_dispatch()
        parked.set()
        await asyncio.Future()

    monkeypatch.setattr(provider, "create_invoice", interrupted_create)
    monkeypatch.setattr(payment, "gateway", lambda: provider)
    running = asyncio.create_task(start(database, order_id, token))
    await asyncio.wait_for(parked.wait(), timeout=10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    state = await cancellation(database, order_id)
    if after_fence:
        assert state.reason == "invoice_dispatch_unconfirmed"
        with pytest.raises(HTTPException):
            await cancellation(database, order_id, state.revision)
        assert await stock(database, product_id, order_id) == (4, 1, "pending", 0)
    else:
        assert state.status == "available"
        await cancellation(database, order_id, state.revision)
        assert await stock(database, product_id, order_id) == (4, 0, "cancelled", 1)
    assert online["posts"] == 0


async def test_legacy_request_without_dispatch_facts_requires_reconciliation(
    database, checkout_dependencies, online, monkeypatch,
):
    order_id, product_id, token = await prepared(database)

    def fail_readiness():
        raise MonobankError("monobank_unavailable")

    online["readiness_hook"] = fail_readiness
    async with database() as session:
        add = session.add

        def add_legacy(obj):
            if not isinstance(obj, ShopOperationAudit) or obj.operation != "online_dispatch_prepared":
                add(obj)

        monkeypatch.setattr(session, "add", add_legacy)
        await payment.start(session, order_id, None, token, str(uuid4()))
    state = await cancellation(database, order_id)
    assert state.status == "reconciliation_required" and state.reason == "invoice_dispatch_unconfirmed"
    with pytest.raises(HTTPException):
        await cancellation(database, order_id, state.revision)
    assert await stock(database, product_id, order_id) == (4, 1, "pending", 0)
    assert online["posts"] == 0


async def test_admin_http_contract_rejects_missing_inventory_permission(
    database, checkout_dependencies, online,
):
    order_id, product_id, _ = await prepared(database)
    async with database() as session:
        employee = AdminUser(email="synthetic-limited@example.test", hashed_password="unused",
            is_active=True, is_superuser=False, can_record_sales=True, can_post_inventory=False)
        session.add(employee)
        await session.commit()

    app = FastAPI()
    app.include_router(backoffice_router)

    async def db():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = lambda: employee
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/shop-payments/orders/{order_id}/online-payment/cancellation"
        response = await client.post(url, headers={"Idempotency-Key": str(uuid4())}, json={"expected_revision": 1})
        assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
        assert await stock(database, product_id, order_id) == (4, 1, "pending", 0)
        app.dependency_overrides[get_current_admin_user] = lambda: ADMIN
        read = await client.get(url)
        assert read.status_code == 200 and read.json()["status"] == "available"
        done = await client.post(url, headers={"Idempotency-Key": str(uuid4())},
            json={"expected_revision": read.json()["revision"]})
        assert done.status_code == 200 and done.json()["status"] == "cancelled"
    assert await stock(database, product_id, order_id) == (4, 0, "cancelled", 1)
    assert online["calls"] == []
