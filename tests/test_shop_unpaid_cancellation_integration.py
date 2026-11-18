"""Bank deactivation and inventory cancellation use separate durable steps."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_online_payment import OnlinePaymentCancellationCommand
from app.services import shop_unpaid_cancellation as service
from app.services.monobank_provider import MonobankError, MonobankGateway
from app.services.order import OrderService
from tests.test_order_owner_archive_integration import command as archive
from tests.test_shop_fiscal_integration import anyio_backend, checkout_dependencies, database, template_database
from tests.test_shop_online_payment_integration import online, prepared, start

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("outcome", ["race", "lost", "local_failure", "archive", "preflight"])
async def test_deactivation_durability(database, checkout_dependencies, online, monkeypatch, outcome):
    order_id, product_id, token = await prepared(database)
    assert (await start(database, order_id, token)).status == "waiting"
    online["status"] = "created"
    online["final_amount"] = 0
    if outcome == "archive":
        state = await archive(database, order_id)
        await archive(database, order_id, state.revision)
    async with database() as session:
        order = await session.get(Order, order_id)
        payload = OnlinePaymentCancellationCommand(expected_revision=order.revision)
        reserved = (await session.get(Product, product_id)).reserved_quantity
    calls = []
    entered, finish = asyncio.Event(), asyncio.Event()

    async def deactivate(self, invoice_id, *, reference, amount_minor, before_dispatch):
        if outcome == "preflight" and not entered.is_set():
            entered.set()
            raise MonobankError("monobank_unavailable")
        await before_dispatch()
        calls.append(invoice_id)
        entered.set()
        if outcome == "race":
            await finish.wait()
        if outcome == "lost":
            raise MonobankError("monobank_invoice_removal_unconfirmed")
        return {"removal_http_status": 200, "removed_at": datetime.now(UTC).isoformat()}

    monkeypatch.setattr(service, "MonobankGateway", lambda **kwargs: MonobankGateway(transport=online["transport"], **kwargs))
    monkeypatch.setattr(MonobankGateway, "deactivate_unpaid_shop_invoice", deactivate)
    async def cancel():
        async with database() as session:
            return await service.cancel(session, SimpleNamespace(id=1), order_id, payload, bank_only=outcome == "archive")

    if outcome == "race":
        first = asyncio.create_task(cancel())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(HTTPException):
                await asyncio.wait_for(cancel(), 5)
        finally:
            finish.set()
            await first
    elif outcome == "lost":
        for _ in range(2):
            with pytest.raises(HTTPException):
                await cancel()
    elif outcome == "local_failure":
        async def crash(self, session, identifier, *args, **kwargs):
            assert await session.scalar(select(func.shop_online_cancel_authorized(identifier, payload.expected_revision)))
            assert not await session.scalar(select(func.shop_online_cancel_authorized(identifier, payload.expected_revision + 1)))
            raise RuntimeError("synthetic local failure")
        with monkeypatch.context() as patch:
            patch.setattr(OrderService, "update_status", crash)
            with pytest.raises(RuntimeError, match="synthetic local failure"):
                await cancel()
        async with database() as session:
            assert (await session.get(Product, product_id)).reserved_quantity == reserved
            assert not await session.scalar(select(func.shop_online_cancel_authorized(order_id, payload.expected_revision)))
        await cancel()
    elif outcome == "preflight":
        with pytest.raises(HTTPException) as error:
            await cancel()
        assert error.value.detail["code"] == "invoice_removal_preflight_failed"
        assert calls == []
        await cancel()
    else:
        await cancel()
    if outcome != "lost":
        assert (await cancel())["invoice_status"] == "deactivated"
    assert len(calls) == 1
    async with database() as session:
        assert (await session.get(Product, product_id)).reserved_quantity == (reserved if outcome == "lost" else 0)
        assert (await session.get(Order, order_id)).status == (OrderStatus.pending if outcome == "lost" else OrderStatus.cancelled)
        if outcome not in {"lost", "archive"}:
            audit = await session.scalar(select(ShopOperationAudit).where(
                ShopOperationAudit.order_id == order_id, ShopOperationAudit.operation == "online_unpaid_finalize",
            ))
            assert audit.facts["expected_revision"] == payload.expected_revision
            assert not await session.scalar(select(func.shop_online_cancel_authorized(order_id, payload.expected_revision + 1)))
        count = await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.order_id == order_id, InventoryMovement.movement_type == InventoryMovementType.reservation_release,
        ))
        assert count == (0 if outcome == "lost" else 1)


@pytest.mark.parametrize("expired_case", ["expired", "processing", "wrong_amount", "paid", "recorded_payment"])
async def test_reconcile_dispatched_invoice_by_status(database, checkout_dependencies, online, monkeypatch, expired_case):
    order_id, product_id, token = await prepared(database)
    assert (await start(database, order_id, token)).status == "waiting"
    online["status"] = "created"
    online["final_amount"] = 0
    calls = []

    async def lost(self, invoice_id, *, reference, amount_minor, before_dispatch):
        await before_dispatch()
        calls.append(invoice_id)
        raise MonobankError("monobank_invoice_removal_unconfirmed")

    monkeypatch.setattr(service, "MonobankGateway", lambda **kwargs: MonobankGateway(transport=online["transport"], **kwargs))
    monkeypatch.setattr(MonobankGateway, "deactivate_unpaid_shop_invoice", lost)
    async with database() as session:
        order = await session.get(Order, order_id)
        payload = OnlinePaymentCancellationCommand(expected_revision=order.revision)
        reserved = (await session.get(Product, product_id)).reserved_quantity

    async def cancel():
        async with database() as session:
            return await service.cancel(session, SimpleNamespace(id=1), order_id, payload)

    with pytest.raises(HTTPException):
        await cancel()
    online["status"] = "processing" if expired_case == "processing" else "expired"
    if expired_case == "wrong_amount":
        online["amount"] += 1
    if expired_case == "paid":
        online["final_amount"] = online["amount"]

    if expired_case == "recorded_payment":
        async with database() as session:
            session.add(OrderPayment(
                order_id=order_id, amount_minor=online["amount"], method="card",
                source="test", evidence="synthetic", operator_id=1,
                simulation=False, created_at=datetime.now(UTC),
            ))
            await session.commit()

    for _ in range(2):
        if expired_case == "expired":
            assert (await cancel())["invoice_status"] == "expired"
        else:
            with pytest.raises(HTTPException):
                await cancel()

    assert len(calls) == 1
    async with database() as session:
        assert (await session.get(Order, order_id)).status == (
            OrderStatus.cancelled if expired_case == "expired" else OrderStatus.pending)
        assert (await session.get(Product, product_id)).reserved_quantity == (
            0 if expired_case == "expired" else reserved)
        count = await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.order_id == order_id, InventoryMovement.movement_type == InventoryMovementType.reservation_release,
        ))
        assert count == int(expired_case == "expired")
