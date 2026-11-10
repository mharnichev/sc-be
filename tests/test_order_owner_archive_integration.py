"""Backoffice owner closure preserves evidence and releases only owned stock on PostgreSQL."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes.orders import backoffice_router
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderItem, OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shipment import Shipment
from app.models.shop_mono_checkbox import ShopMonoCheckboxReceipt
from app.models.shop_sales import ShopOperationAudit
from app.schemas.fiscal import MockShiftCommand
from app.schemas.fiscal_returns import MockReportCreate
from app.schemas.order import OrderCancellationCommand
from app.schemas.shipment import RealShipmentAttach
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import customer_tracking, fiscal, fiscal_returns, mono_checkbox_poll, order_archive, shop_online_payment
from app.services.inventory import InventoryService
from tests.test_fiscal_integration import actor as fiscal_actor, approval, command as fiscal_command, preview, submit
from tests.test_mock_cod_integration import confirm as confirm_cod, delivered as delivered_cod
from tests.test_shop_fiscal_integration import anyio_backend, checkout_dependencies, database, template_database
from tests.test_shop_online_payment_integration import online, prepared, start
from tests.test_shop_payment_resume_integration import create_order
from tests.test_shop_sales_integration import attach, execute, seed

pytestmark = pytest.mark.anyio
ADMIN = SimpleNamespace(id=1, is_active=True, is_superuser=True)


async def command(database, identifier, revision=None, *, key=None, actor=ADMIN, comment=None):
    async with database() as session:
        if revision is None:
            return await order_archive.read(session, actor, identifier)
        return await order_archive.cancel(session, actor, identifier,
            OrderCancellationCommand(expected_revision=revision, comment=comment), key or str(uuid4()))


async def stock(database, product_id, order_id):
    async with database() as session:
        product = await session.get(Product, product_id)
        order = await session.get(Order, order_id)
        releases = await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.order_id == order_id, InventoryMovement.reference_key.endswith(":owner-archive-release")))
        return product.stock_quantity, product.reserved_quantity, order.status, releases


async def test_owner_api_archive_search_direct_links_revision_and_replay(database, checkout_dependencies, online):
    identifier, product_id, token = await prepared(database)
    other, _ = await create_order(database, product_id)
    historical, _, _ = await prepared(database)
    async with database() as session:
        await session.execute(text("UPDATE orders SET status='cancelled', customer_name='Historical fixture' WHERE id=:id"),
            {"id": historical})
        await session.commit()
        denied = AdminUser(email="archive-denied@example.test", hashed_password="unused", is_active=True,
            is_superuser=False, can_record_sales=True, can_post_inventory=False)
        session.add(denied)
        master = AdminUser(email="archive-master@example.test", hashed_password="unused", is_active=True,
            is_superuser=False, can_record_sales=True, can_post_inventory=True)
        session.add(master)
        await session.flush()
        session.add(Master(full_name="Synthetic archive master", admin_user_id=master.id))
        await session.commit()

    app = FastAPI()
    app.include_router(backoffice_router, prefix="/orders")

    async def db():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = lambda: denied
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/orders/{identifier}/cancellation"
        headers = {"Idempotency-Key": str(uuid4())}
        assert (await client.post(url, headers=headers, json={"expected_revision": 1})).status_code == 403
        app.dependency_overrides[get_current_admin_user] = lambda: master
        assert (await client.post(url, headers=headers, json={"expected_revision": 1})).status_code == 403
        app.dependency_overrides[get_current_admin_user] = lambda: ADMIN
        initial = (await client.get(url)).json()
        assert initial["can_cancel"] and initial["active_reservation_quantity"] == 1
        conflict = await client.post(url, headers=headers, json={"expected_revision": initial["revision"] + 1})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["cancellation"] == initial
        payload = {"expected_revision": initial["revision"], "comment": " owner cancelled "}
        results = await asyncio.gather(*(client.post(url, headers=headers, json=payload) for _ in range(2)))
        assert [response.status_code for response in results] == [200, 200]
        first, second = [response.json() for response in results]
        assert first == second and first["archived"] and first["comment"] == "owner cancelled"
        assert first["released_reservation_quantity"] == 1 and first["active_reservation_quantity"] == 0
        assert first["order_status"] == "cancelled" and first["payment_status"] == "unpaid"
        assert first["cancelled_by_user_id"] == 1 and not any(first["reconciliation"].values())
        assert (await client.get(f"/orders/{identifier}")).json()["archived"]
        assert (await client.get("/orders?archive=active")).json()["total"] == 1
        assert (await client.get("/orders?archive=archived")).json()["total"] == 2
        by_id = (await client.get("/orders", params={"archive": "archived", "search": str(identifier)})).json()
        assert identifier in [row["id"] for row in by_id["items"]]
        by_name = (await client.get("/orders", params={"archive": "archived", "search": "Historical"})).json()
        assert [row["id"] for row in by_name["items"]] == [historical]
        assert (await client.get("/orders?archive=archived&search=%25")).json()["total"] == 0
        assert (await client.get("/orders")).json()["total"] == 3
        assert (await client.get(f"/orders/{historical}/cancellation")).json()["cancelled_at"] is None
        assert (await client.get("/orders/archive-metrics")).json()["cancellations_total"] == 1

        patch = await client.patch(f"/orders/{other}/status", headers={"Idempotency-Key": str(uuid4())},
            json={"status": "cancelled", "expected_revision": 1})
        assert patch.status_code == 200 and patch.json()["archived"]
    assert await stock(database, product_id, identifier) == (4, 0, OrderStatus.cancelled, 1)
    assert await stock(database, product_id, other) == (4, 0, OrderStatus.cancelled, 1)
    with pytest.raises(HTTPException) as closed:
        await start(database, identifier, token)
    assert closed.value.status_code == 409
    assert online["calls"] == []


@pytest.mark.parametrize("handover", [False, True])
async def test_paid_archive_preserves_original_receipt_and_never_restocks(database, checkout_dependencies, online, handover):
    identifier, product_id, token = await prepared(database)
    unrelated, _ = await create_order(database, product_id)
    await start(database, identifier, token)
    online["status"] = "success"
    online["checks"] = [dict(id=str(uuid4()), status="done", type="sale", fiscalizationSource="checkbox")]
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 1, "blocked": 0}

    if handover:
        state = await command(database, identifier)
        await execute(database, identifier, ShopPhysicalCommand(expected_revision=state.revision, evidence="synthetic pickup"), "handover")
        state = await command(database, identifier)
        await execute(database, identifier, ShopPhysicalCommand(expected_revision=state.revision, evidence="synthetic completed"), "complete")

    async def evidence():
        async with database() as session:
            payment = await session.scalar(select(OrderPayment).where(OrderPayment.order_id == identifier))
            receipt = await session.scalar(select(ShopMonoCheckboxReceipt).where(ShopMonoCheckboxReceipt.order_id == identifier))
            return payment.id, payment.amount_minor, receipt.receipt_id, receipt.proof_hash

    original = await evidence()
    calls = list(online["calls"])
    state = await command(database, identifier)
    if handover:
        assert state.order_status == OrderStatus.completed
    result = await command(database, identifier, state.revision)
    assert result.archived and result.reconciliation.payment and result.reconciliation.receipt
    assert result.reconciliation.inventory is handover
    assert result.payment_status == "simulated_paid"
    assert result.fiscal_status == "simulated_mono_checkbox_external_accepted"
    assert result.released_reservation_quantity == int(not handover)
    assert await stock(database, product_id, identifier) == (4 - int(handover), 1, OrderStatus.cancelled, int(not handover))
    assert await stock(database, product_id, unrelated) == (4 - int(handover), 1, OrderStatus.pending, 0)
    assert await evidence() == original and online["calls"] == calls
    assert result.fulfillment_status == ("simulated_fulfilled" if handover else "cancelled")
    async with database() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE orders SET status='pending' WHERE id=:id"), {"id": identifier})
        await session.rollback()


async def test_uncertain_invoice_allows_cancel_while_provider_is_unavailable(database, checkout_dependencies, online, monkeypatch):
    identifier, product_id, token = await prepared(database)
    online["unknown"] = True
    payment = await start(database, identifier, token)
    assert payment.status == "creation_unknown"

    def reject():
        raise AssertionError("Cancellation must not invoke Mono")

    monkeypatch.setattr(shop_online_payment, "gateway", reject)
    calls = list(online["calls"])
    state = await command(database, identifier)
    done = await command(database, identifier, state.revision)
    assert done.archived and done.payment_status == "failed" and done.reconciliation.payment
    assert done.released_reservation_quantity == 1 and online["calls"] == calls
    assert await stock(database, product_id, identifier) == (4, 0, OrderStatus.cancelled, 1)


async def test_archive_releases_the_remaining_partial_reserve(database, checkout_dependencies, online):
    identifier, product_id = await seed(database, method="onlineCard", stock=6, quantity=3)
    other, _ = await create_order(database, product_id)
    async with database() as session:
        item = await session.scalar(select(OrderItem).where(OrderItem.order_id == identifier))
        product = await session.get(Product, product_id)
        product.reserved_quantity -= 1
        session.add(InventoryMovement(product_id=product_id, order_id=identifier, order_item_id=item.id,
            movement_type=InventoryMovementType.reservation_release, quantity=1, on_hand_delta=0,
            reserved_delta=-1, performed_by_user_id=1, reference_key="synthetic-earlier-partial-release"))
        await session.commit()
    state = await command(database, identifier)
    assert state.active_reservation_quantity == 2
    result = await command(database, identifier, state.revision)
    assert result.released_reservation_quantity == 2 and not result.reconciliation.inventory
    assert await stock(database, product_id, identifier) == (6, 1, OrderStatus.cancelled, 1)
    assert await stock(database, product_id, other) == (6, 1, OrderStatus.pending, 0)


async def test_archive_does_not_modify_the_active_shipment(database, checkout_dependencies, online):
    identifier, product_id = await seed(database, kind="nova_poshta", method="cashOnDelivery")
    shipment_id, number = await attach(database, identifier)
    async with database() as session:
        order = await session.get(Order, identifier)
        order.fulfillment_method = None
        order.shipping_company = "novaPost"
        restricted = AdminUser(email="archive-no-shipment@example.test", hashed_password="unused", is_active=True,
            is_superuser=False, can_record_sales=True, can_post_inventory=True)
        session.add(restricted)
        await session.commit()
    with pytest.raises(HTTPException) as forbidden:
        await command(database, identifier, revision=1, actor=restricted)
    assert forbidden.value.status_code == 403
    state = await command(database, identifier)
    result = await command(database, identifier, state.revision)
    assert result.archived and result.reconciliation.inventory
    assert result.released_reservation_quantity == 1
    assert await stock(database, product_id, identifier) == (4, 0, OrderStatus.cancelled, 1)
    async with database() as session:
        shipment = await session.get(Shipment, shipment_id)
        assert shipment.active and shipment.status == "requires_review" and shipment.tracking_number == number
        with pytest.raises(HTTPException) as closed:
            await customer_tracking.attach_existing(session, ADMIN, identifier,
                RealShipmentAttach(expected_revision=result.revision, tracking_number="00000000000001"))
        assert closed.value.status_code == 409
    assert online["calls"] == []


async def test_archive_keeps_issued_cod_receipt_readable(database, checkout_dependencies, monkeypatch):
    for flag in ("np_mock_shipments_enabled", "checkbox_mock_enabled", "local_sales_simulation_enabled"):
        monkeypatch.setattr(settings, flag, True)
    order, shipment_id, product_id = await delivered_cod(database, checkout_dependencies)
    await confirm_cod(database, order, shipment_id)
    async with database() as session:
        await fiscal.shift(session, await fiscal_actor(session), fiscal.REGISTER,
            MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
    issued = await fiscal_command(database, await submit(database, order.id,
        approval(await preview(database, order.id))), "process")
    state = await command(database, order.id)
    result = await command(database, order.id, state.revision)
    assert result.archived and result.payment_status == "simulated_paid"
    assert await stock(database, product_id, order.id) == (4, 0, OrderStatus.cancelled, 0)
    async with database() as session:
        historical = await fiscal.get_document(session, await fiscal_actor(session), issued.id)
        assert historical.status == "issued" and historical.snapshot == issued.snapshot
    with pytest.raises(HTTPException) as closed:
        await preview(database, order.id)
    assert closed.value.status_code == 409
    async with database() as session:
        actor = await fiscal_actor(session)
        reports = [await fiscal_returns.report(session, actor, fiscal.REGISTER,
            MockReportCreate(kind=kind, expected_shift_revision=1, confirm=True), str(uuid4())) for kind in ("x", "z")]
        assert reports[0].totals == reports[1].totals
        assert (reports[0].totals.sales_minor, reports[0].totals.returns_minor) == (10000, 0)


async def test_archive_rejects_stale_procurement_command(database, checkout_dependencies, online):
    identifier, _ = await seed(database, method="onlineCard", stock=0, backorder=True)
    state = await command(database, identifier)
    assert (await command(database, identifier, state.revision)).archived
    async with database() as session:
        item = await session.scalar(select(OrderItem).where(OrderItem.order_id == identifier))
        with pytest.raises(HTTPException, match="409"):
            await InventoryService().mark_ordered(session, [item.id], ADMIN.id)
    assert online["calls"] == []


async def test_inventory_mismatch_cannot_release_another_order_and_telemetry_failure_isolated(
    database, checkout_dependencies, online, monkeypatch,
):
    identifier, product_id, _ = await prepared(database)
    other, _ = await create_order(database, product_id)
    async with database() as session:
        await session.execute(text("UPDATE products SET reserved_quantity=1 WHERE id=:id"), {"id": product_id})
        await session.commit()

    reported = asyncio.Event()
    loop = asyncio.get_running_loop()

    def fail_reporting(*args, **kwargs):
        loop.call_soon_threadsafe(reported.set)
        raise RuntimeError("synthetic reporting outage")

    monkeypatch.setattr(order_archive.error_reporting, "report", fail_reporting)
    state = await command(database, identifier)
    result = await command(database, identifier, state.revision)
    await asyncio.wait_for(reported.wait(), timeout=5)
    assert result.archived and result.reconciliation.inventory
    assert result.released_reservation_quantity == 0
    assert await stock(database, product_id, identifier) == (4, 1, OrderStatus.cancelled, 0)
    assert await stock(database, product_id, other) == (4, 1, OrderStatus.pending, 0)
    async with database() as session:
        audit = await session.scalar(select(ShopOperationAudit).where(ShopOperationAudit.operation == "owner_cancel_order"))
        assert audit.facts["inventory_reconciliation"]
        assert (await order_archive.metrics(session))["reconciliation_total"] == 1
