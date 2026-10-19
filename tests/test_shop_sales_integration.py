"""Shop operations against a disposable migrated PostgreSQL database, no provider I/O."""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.routes import orders, shop_sales as routes
from app.core.config import Settings, settings
from app.core.database import get_db_session
from app.core.security import create_access_token
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderHandover, OrderIntent, OrderItem, OrderPayment, OrderStatus, ProcurementStatus
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentItem, TrackingEvent
from app.models.shop_sales import ShopCompletion, ShopFulfillmentFact, ShopOperationAudit, ShopPaymentConfirmation
from app.schemas.order import OrderCreate
from app.schemas.shop_sales import ManualShopPayment, ProviderShopPayment, ShopPhysicalCommand
from app.services import np_tracking, shop_sales
from app.services.order import OrderService
from app.services.inventory import InventoryService
from tests.test_inventory_integration import checkout_dependencies  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
async def database():
    raw_url = os.environ.get("INVENTORY_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set the isolated local PostgreSQL test URL")
    parsed = make_url(raw_url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"} and "test" in parsed.database
    name = "shop_ops_test_" + uuid4().hex
    child_url = parsed.set(database=name).render_as_string(hide_password=False)
    admin = create_async_engine(raw_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    previous = settings.database_url
    settings.database_url = child_url
    engine = create_async_engine(child_url)
    try:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        await asyncio.to_thread(alembic_command.upgrade, config, "head")
        # Prove the new empty schema roundtrip before accepting immutable facts.
        await asyncio.to_thread(alembic_command.downgrade, config, "0121_mock_cod_settlement")
        await asyncio.to_thread(alembic_command.upgrade, config, "head")
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(AdminUser(email="shop-operator@example.test", hashed_password="unused",
                                  is_active=True, is_superuser=True))
            await session.commit()
        yield sessions
        with pytest.raises(RuntimeError, match="Accepted Shop history"):
            await asyncio.to_thread(alembic_command.downgrade, config, "0121_mock_cod_settlement")
    finally:
        settings.database_url = previous
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}"'))
        await admin.dispose()


@pytest.fixture(autouse=True)
def isolated_flags(monkeypatch):
    monkeypatch.setattr(settings, "shop_order_operations_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(shop_sales, "payment_adapter", shop_sales.UnavailablePaymentAdapter())
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.UnavailableFiscalPolicy())


async def seed(database, *, kind="pickup", method="bank_transfer", stock=4, quantity=1, backorder=False):
    async with database() as session:
        product = Product(name="Synthetic Shop item", slug=uuid4().hex, price=Decimal("100.00"),
                          stock_quantity=stock, reserved_quantity=0, allow_backorder=backorder)
        session.add(product)
        await session.commit()
        order = await OrderService().create_order(session, OrderCreate(firstName="Synthetic", phoneNumber="+380500001234",
            shippingMethod="pickup", paymentMethod=method, items=[{"productId": product.id, "quantity": quantity}]))
        order.fulfillment_method = kind
        await session.commit()
        return order.id, product.id


async def attach(database, order_id, *, include_items=True):
    async with database() as session:
        order = await shop_sales.load_order(session, order_id)
        number = str(uuid4().int % 10**14).zfill(14)
        shipment = Shipment(order_id=order_id, simulation=False, carrier="nova_poshta", source="attach", active=True,
            operation_uuid=str(uuid4()), provider_reference=number, tracking_number=number,
            status="requires_review", raw_status="", progress=0, address_snapshot={}, packaging={}, created_at=datetime.now(UTC))
        session.add(shipment)
        await session.flush()
        if include_items:
            session.add_all([ShipmentItem(shipment_id=shipment.id, order_item_id=i.id, quantity=i.quantity) for i in order.items])
        await session.commit()
        return shipment.id, number


async def delivered(database, shipment_id):
    async with database() as session:
        shipment = await session.get(Shipment, shipment_id)
        event_at = datetime.now(UTC) - timedelta(minutes=1)
        for offset in (1, 0):
            await np_tracking.apply_observation(session, shipment,
                np_tracking.Observation(shipment.tracking_number, "9", event_at), datetime.now(UTC) - timedelta(seconds=offset))
            await session.flush()
        await session.commit()


def manual(revision=1, *, method="bank_transfer", reference=None, **extra):
    return ManualShopPayment(expected_revision=revision, amount_minor=10000, currency="UAH", method=method,
        external_reference=reference or uuid4().hex, evidence="Synthetic independently verified staff evidence", **extra)


async def execute(database, order_id, payload, operation, *, key=None, actor_id=1):
    async with database() as session:
        actor = await session.get(AdminUser, actor_id)
        return await shop_sales.command(session, actor, order_id, payload, key or str(uuid4()), operation)


async def state(database, order_id):
    async with database() as session:
        return await shop_sales.read(session, await session.get(AdminUser, 1), order_id)


async def stock(database, product_id):
    async with database() as session:
        product = await session.get(Product, product_id)
        return product.stock_quantity, product.reserved_quantity


async def counts(database, order_id):
    async with database() as session:
        return {model.__tablename__: await session.scalar(select(func.count()).select_from(model).where(model.order_id == order_id))
                for model in (OrderPayment, OrderHandover, ShopPaymentConfirmation, ShopFulfillmentFact,
                              ShopCompletion, ShopOperationAudit, InventoryMovement)}


async def test_manual_pickup_separate_stages_replay_and_atomic_inventory(database, checkout_dependencies, monkeypatch):
    order_id, product_id = await seed(database)
    assert not settings.shop_checkout_enabled
    request, key = manual(), str(uuid4())
    first, second = await asyncio.gather(*[execute(database, order_id, request, "manual_payment", key=key) for _ in range(2)])
    assert first.model_dump() == second.model_dump()
    assert first.stage == "paid" and first.order.payment_status == "paid_manual"
    assert await stock(database, product_id) == (4, 1)
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, ShopPhysicalCommand(expected_revision=2, evidence="fixture"), "handover")
    assert error.value.detail["code"] == "fiscal_policy_unavailable"
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    hand_payload, hand_key = ShopPhysicalCommand(expected_revision=2, evidence="Synthetic pickup"), str(uuid4())
    handed, replay = await asyncio.gather(*[execute(database, order_id, hand_payload, "handover", key=hand_key) for _ in range(2)])
    assert handed.model_dump() == replay.model_dump()
    assert handed.stage == "dispatched" and handed.authority == "test"
    assert handed.order.status == OrderStatus.paid
    assert handed.order.fulfillment_status == "simulated_pickup_handed_over"
    assert await stock(database, product_id) == (3, 0)
    complete_request, complete_key = ShopPhysicalCommand(expected_revision=3, evidence="Synthetic completed pickup"), str(uuid4())
    complete = await execute(database, order_id, complete_request, "complete", key=complete_key)
    assert complete.stage == "completed" and complete.order.status == OrderStatus.completed
    assert complete.authority == "test" and complete.order.fulfillment_status == "simulated_fulfilled"
    assert await stock(database, product_id) == (3, 0)
    assert await counts(database, order_id) == {"order_payments": 1, "order_handovers": 1,
        "shop_payment_confirmations": 1, "shop_fulfillment_facts": 1, "shop_completions": 1,
        "shop_operation_audits": 3, "inventory_movements": 2}
    monkeypatch.setattr(settings, "shop_order_operations_enabled", False)
    assert (await execute(database, order_id, complete_request, "complete", key=complete_key)).model_dump() == complete.model_dump()
    closed = await state(database, order_id)
    assert closed.actions["replay_complete"] and not closed.actions["complete"]
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, manual(revision=4), "manual_payment")
    assert error.value.status_code == 503


async def test_global_reference_race_across_orders_has_one_effect(database, checkout_dependencies):
    orders_ = [await seed(database) for _ in range(2)]
    request = manual(reference="GLOBAL-" + uuid4().hex)
    async def attempt(order_id):
        try:
            return await execute(database, order_id, request, "manual_payment")
        except HTTPException as error:
            return error
    results = await asyncio.wait_for(asyncio.gather(*(attempt(order_id) for order_id, _ in orders_)), 15)
    assert sum(not isinstance(result, HTTPException) for result in results) == 1
    loser = next(result for result in results if isinstance(result, HTTPException))
    assert loser.status_code == 409 and loser.detail["code"] == "external_reference_used"
    assert sum([(await counts(database, order_id))["order_payments"] for order_id, _ in orders_]) == 1
    for _, product_id in orders_:
        assert await stock(database, product_id) == (4, 1)


@pytest.mark.parametrize("payment_method", ["cashOnDelivery", "cardOnDelivery"])
async def test_cod_dispatch_then_confirm_delivered_money_then_complete(database, checkout_dependencies, monkeypatch, payment_method):
    order_id, product_id = await seed(database, kind="nova_poshta", method=payment_method)
    shipment_id, number = await attach(database, order_id)
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    dispatched = await execute(database, order_id, ShopPhysicalCommand(expected_revision=1, evidence="Synthetic NP dispatch"), "handover")
    assert dispatched.stage == "dispatched" and dispatched.payment is None
    assert await stock(database, product_id) == (3, 0)
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, manual(revision=2, method="card"), "manual_payment")
    assert error.value.detail["code"] == "provider_proof_invalid"
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, manual(revision=2, method="cod"), "manual_payment")
    assert error.value.detail["code"] in {"tracking_stale", "tracking_delivery_required"}
    await delivered(database, shipment_id)
    paid = await execute(database, order_id, manual(revision=2, method="cod"), "manual_payment")
    assert paid.payment.method == "cod" and paid.authority == "test"
    async with database() as session:
        proof = await session.scalar(select(ShopPaymentConfirmation).where(ShopPaymentConfirmation.order_id == order_id))
        assert proof.snapshot["original_payment_method"] == payment_method
        assert await session.scalar(select(func.count()).select_from(TrackingEvent).where(TrackingEvent.shipment_id == shipment_id)) == 1
    complete = await execute(database, order_id, ShopPhysicalCommand(expected_revision=3, evidence="Synthetic COD settlement complete"), "complete")
    assert complete.stage == "completed" and complete.fulfillment.tracking_number == number
    assert await stock(database, product_id) == (3, 0)


async def test_provider_verifies_server_amount_and_cannot_promote_test_authority(database, checkout_dependencies, monkeypatch):
    order_id, product_id = await seed(database)
    reference = "PROVIDER-" + uuid4().hex
    request = ProviderShopPayment(expected_revision=1, reference=reference)
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, request, "provider_payment")
    assert error.value.detail["code"] == "provider_unavailable"
    proof = shop_sales.VerifiedPayment(reference, order_id, 10000, "UAH", "bank_transfer", None, "verified-test", "test")
    adapter = shop_sales.IsolatedTestPaymentAdapter({reference: proof})
    monkeypatch.setattr(shop_sales, "payment_adapter", adapter)
    for change in ({"amount_minor": 1}, {"order_id": order_id+999}, {"currency": "USD"},
                   {"shipment_reference": "wrong"}, {"authority": "provider"}):
        adapter.records[reference] = replace(proof, **change)
        with pytest.raises(HTTPException) as error:
            await execute(database, order_id, request, "provider_payment")
        assert error.value.detail["code"] == "provider_proof_invalid"
    adapter.records[reference] = proof
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, request, "provider_payment")
    assert error.value.detail["code"] == "provider_test_unsafe"
    monkeypatch.setattr(settings, "app_env", "test")
    paid = await execute(database, order_id, request, "provider_payment")
    assert paid.payment.authority == "test" and paid.order.payment_status == "simulated_paid"
    assert await stock(database, product_id) == (4, 1)


async def test_http_closed_permissions_and_strict_provider_payload(database, checkout_dependencies, monkeypatch):
    order_id, _ = await seed(database)
    app = FastAPI()
    app.include_router(routes.backoffice_router)
    app.include_router(orders.public_router, prefix="/public/orders")
    async def isolated_session():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = isolated_session
    auth = {"Authorization": "Bearer " + create_access_token(1), "Idempotency-Key": str(uuid4())}
    base = f"/orders/{order_id}/shop-operations"
    monkeypatch.setattr(settings, "shop_order_operations_enabled", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shop-ops.test") as client:
        assert (await client.get(base)).status_code == 401
        before = await counts(database, order_id)
        result = await client.post(base + "/payments/manual", headers=auth, json=manual().model_dump())
        assert result.status_code == 503 and result.json()["detail"]["code"] == "shop_operations_disabled"
        assert (await client.get(base, headers=auth)).status_code == 200
        invalid = await client.post(base + "/payments/provider", headers=auth, json={"expected_revision": 1, "reference": "x", "paid": True, "amount_minor": 10000})
        assert invalid.status_code == 422
        closed = await client.post("/public/orders", headers=auth, json={"firstName": "Synthetic", "phoneNumber": "+380500001234", "items": [{"productId": 1, "quantity": 1}]})
        assert closed.status_code == 503 and closed.json()["detail"]["code"] == "shop_checkout_closed"
        assert await counts(database, order_id) == before


async def test_changed_body_revoked_permission_cancellation_and_immutability(database, checkout_dependencies):
    order_id, _ = await seed(database)
    async with database() as session:
        actor = AdminUser(email=uuid4().hex+"@example.test", hashed_password="unused", is_active=True,
                          is_superuser=False, can_record_sales=True)
        session.add(actor)
        await session.commit()
        actor_id = actor.id
    request, key = manual(), str(uuid4())
    await execute(database, order_id, request, "manual_payment", key=key, actor_id=actor_id)
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, request.model_copy(update={"evidence": "changed"}), "manual_payment", key=key, actor_id=actor_id)
    assert error.value.detail["code"] == "idempotency_conflict"
    async with database() as session:
        actor = await session.get(AdminUser, actor_id)
        actor.can_record_sales = False
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, request, "manual_payment", key=key, actor_id=actor_id)
    assert error.value.status_code == 403
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await OrderService().update_status(session, order_id, OrderStatus.cancelled, admin_user_id=1)
        assert error.value.detail["code"] == "shop_refund_required"
    async with database() as session:
        with pytest.raises(Exception, match="immutable"):
            await session.execute(text("UPDATE shop_payment_confirmations SET external_reference='tamper' WHERE order_id=:id"), {"id": order_id})
        await session.rollback()


@pytest.mark.parametrize("bad", [{"expected_revision": True}, {"expected_revision": 0}, {"amount_minor": True},
                                 {"amount_minor": 100.0}, {"external_reference": "  "}, {"evidence": "  "}, {"currency": "USD"}])
async def test_strict_payment_contract(bad):
    values = manual().model_dump()
    values.update(bad)
    with pytest.raises(ValidationError):
        ManualShopPayment(**values)


async def test_operations_default_closed_every_environment(monkeypatch):
    monkeypatch.delenv("SHOP_ORDER_OPERATIONS_ENABLED", raising=False)
    for env in ("local", "development", "test", "staging", "production"):
        config = Settings(_env_file=None, APP_ENV=env, DEBUG=False, SECRET_KEY="synthetic-operations-long-enough-secret",
                          CORS_ORIGINS="https://soulcuts.com.ua")
        assert not config.shop_order_operations_enabled


async def test_same_order_different_keys_and_uncommitted_failure_are_atomic(database, checkout_dependencies, monkeypatch):
    order_id, product_id = await seed(database)
    async def attempt():
        try:
            return await execute(database, order_id, manual(), "manual_payment")
        except HTTPException as error:
            return error
    results = await asyncio.wait_for(asyncio.gather(attempt(), attempt()), 15)
    assert sum(not isinstance(result, HTTPException) for result in results) == 1
    assert next(result for result in results if isinstance(result, HTTPException)).detail["code"] == "revision_conflict"
    assert (await counts(database, order_id))["order_payments"] == 1
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    original = OrderService.apply_handover_inventory
    def fail_after_inventory(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic crash after stock delta")
    monkeypatch.setattr(OrderService, "apply_handover_inventory", staticmethod(fail_after_inventory))
    before = await counts(database, order_id)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        await execute(database, order_id, ShopPhysicalCommand(expected_revision=2, evidence="Synthetic pickup"), "handover")
    assert await stock(database, product_id) == (4, 1)
    assert await counts(database, order_id) == before
    assert (await state(database, order_id)).revision == 2


@pytest.mark.parametrize("condition", ["failed_poll", "stale", "refused", "missing_event"])
async def test_cod_negative_tracking_retains_no_payment_effect(database, checkout_dependencies, monkeypatch, condition):
    order_id, product_id = await seed(database, kind="nova_poshta", method="cardOnDelivery")
    shipment_id, _ = await attach(database, order_id)
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    await execute(database, order_id, ShopPhysicalCommand(expected_revision=1, evidence="Synthetic dispatch"), "handover")
    await delivered(database, shipment_id)
    async with database() as session:
        shipment = await session.get(Shipment, shipment_id)
        if condition == "failed_poll":
            shipment.last_attempt_status = "timeout"
        elif condition == "stale":
            shipment.last_checked_at = datetime.now(UTC) - timedelta(days=2)
        elif condition == "refused":
            shipment.tracking_status = "refused"
        else:
            shipment.provider_event_at = datetime.now(UTC) - timedelta(days=4)
        await session.commit()
    before = await counts(database, order_id)
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, manual(revision=2, method="cod"), "manual_payment")
    assert error.value.detail["code"] in {"tracking_stale", "tracking_ambiguous", "tracking_delivery_required"}
    assert await counts(database, order_id) == before
    assert await stock(database, product_id) == (3, 0)


@pytest.mark.parametrize("defect", ["partial_shipment", "duplicate_shipment", "foreign_reservation", "partial_procurement", "zero_total"])
async def test_handover_requires_exact_shipment_owned_full_inventory(database, checkout_dependencies, monkeypatch, defect):
    kind = "nova_poshta" if "shipment" in defect else "pickup"
    order_id, product_id = await seed(database, kind=kind)
    if "shipment" in defect:
        shipment_id, _ = await attach(database, order_id, include_items=defect != "partial_shipment")
        if defect == "duplicate_shipment":
            await attach(database, order_id)
    if defect == "zero_total":
        async with database() as session:
            order = await session.get(Order, order_id)
            order.total_amount = 0
            await session.commit()
        with pytest.raises(HTTPException) as error:
            await execute(database, order_id, manual(), "manual_payment")
        assert error.value.detail["code"] == "unsupported_zero_total"
        assert (await state(database, order_id)).blocking_reasons.count("unsupported_zero_total") == 1
        return
    if "shipment" in defect:
        with pytest.raises(HTTPException) as error:
            await execute(database, order_id, manual(), "manual_payment")
        assert error.value.detail["code"] in {"carrier_allocation_mismatch", "carrier_shipment_ambiguous"}
        return
    await execute(database, order_id, manual(), "manual_payment")
    async with database() as session:
        item = await session.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        product = await session.get(Product, product_id)
        if defect == "foreign_reservation":
            # Move this order's ledger reservation to an unrelated synthetic owner;
            # keep aggregate product stock reserved so aggregate-only checks would pass.
            movement = await session.scalar(select(InventoryMovement).where(InventoryMovement.order_id == order_id))
            movement.order_id = None
        else:
            item.quantity_from_stock, item.quantity_to_order = 0, 1
            product.reserved_quantity = 0
            movement = await session.scalar(select(InventoryMovement).where(InventoryMovement.order_id == order_id))
            movement.order_id = None
        await session.commit()
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    before = await stock(database, product_id)
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, ShopPhysicalCommand(expected_revision=2, evidence="Synthetic pickup"), "handover")
    assert error.value.detail["code"] in {"reservation_ownership_mismatch", "procurement_required"}
    assert await stock(database, product_id) == before
    assert (await counts(database, order_id))["order_handovers"] == 0


@pytest.mark.parametrize("simulation,source,method", [(False, "other", "cod"), (False, "shop_manual", "cod"),
    (True, "mock_cod_confirmation", "cod"), (True, "shop_manual", "cod"), (False, "shop_provider", "card")])
async def test_migrated_database_rejects_bare_payment_proof(database, checkout_dependencies, simulation, source, method):
    order_id, _ = await seed(database, kind="nova_poshta", method="cashOnDelivery")
    async with database() as session:
        session.add(OrderPayment(order_id=order_id, amount_minor=10000, method=method, source=source,
            evidence="synthetic no provenance", operator_id=1, simulation=simulation, created_at=datetime.now(UTC)))
        with pytest.raises(Exception, match="provenance"):
            await session.commit()
        await session.rollback()
    assert (await counts(database, order_id))["order_payments"] == 0


async def test_preloaded_permissions_refresh_and_fiscal_test_rejected_in_production(database, checkout_dependencies, monkeypatch):
    order_id, _ = await seed(database)
    async with database() as session:
        actor = AdminUser(email=uuid4().hex+"@example.test", hashed_password="unused", is_active=True,
            is_superuser=False, can_record_sales=True, can_post_inventory=True)
        session.add(actor)
        await session.commit()
        actor_id = actor.id
    async with database() as stale:
        actor = await stale.get(AdminUser, actor_id)
        await shop_sales.load_order(stale, order_id)
        async with database() as other:
            current = await other.get(AdminUser, actor_id)
            current.can_record_sales = False
            await other.commit()
        with pytest.raises(HTTPException) as error:
            await shop_sales.command(stale, actor, order_id, manual(), str(uuid4()), "manual_payment")
        assert error.value.status_code == 403
    await execute(database, order_id, manual(), "manual_payment")
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, ShopPhysicalCommand(expected_revision=2, evidence="Synthetic"), "handover")
    assert error.value.detail["code"] == "fiscal_test_unsafe"
    assert not (await state(database, order_id)).actions["handover"]


async def test_valid_original_mock_cod_still_passes_migrated_guard(database, checkout_dependencies, monkeypatch):
    from tests.test_mock_cod_integration import delivered as mock_delivered, confirm
    monkeypatch.setattr(settings, "np_mock_shipments_enabled", True)
    order, shipment_id, _ = await mock_delivered(database, checkout_dependencies)
    paid = await confirm(database, order, shipment_id)
    assert paid.payment_status == "simulated_paid"
    async with database() as session:
        assert await session.scalar(select(ShopPaymentConfirmation.id).where(ShopPaymentConfirmation.order_id == order.id)) is None
        payment = await session.scalar(select(OrderPayment).where(OrderPayment.order_id == order.id))
        assert payment.simulation and payment.source == "mock_cod_confirmation"


async def test_provider_reference_unique_across_different_orders(database, checkout_dependencies, monkeypatch):
    order_ids = [(await seed(database))[0] for _ in range(2)]
    class InconsistentTestGateway(shop_sales.IsolatedTestPaymentAdapter):
        async def verify(self, reference, **expected):
            return shop_sales.VerifiedPayment(reference=reference, **expected, method="bank_transfer",
                                             verification_id="synthetic-inconsistent-provider", authority="test")
    monkeypatch.setattr(shop_sales, "payment_adapter", InconsistentTestGateway({}))
    reference = "UNIQUE-PROVIDER-" + uuid4().hex
    async def attempt(order_id):
        try:
            return await execute(database, order_id, ProviderShopPayment(expected_revision=1, reference=reference), "provider_payment")
        except HTTPException as error:
            return error
    results = await asyncio.wait_for(asyncio.gather(*(attempt(order_id) for order_id in order_ids)), 15)
    assert sum(not isinstance(result, HTTPException) for result in results) == 1
    assert next(result for result in results if isinstance(result, HTTPException)).detail["code"] == "external_reference_used"
    assert sum([(await counts(database, order_id))["shop_payment_confirmations"] for order_id in order_ids]) == 1


async def test_master_denied_and_np_replay_requires_current_shipment_permission(database, checkout_dependencies, monkeypatch):
    order_id, _ = await seed(database, kind="nova_poshta", method="cashOnDelivery")
    await attach(database, order_id)
    async with database() as session:
        actor = AdminUser(email=uuid4().hex+"@example.test", hashed_password="unused", is_active=True,
                          is_superuser=False, can_record_sales=True, can_post_inventory=True, can_manage_shipments=True)
        session.add(actor)
        await session.flush()
        actor_id = actor.id
        master = Master(admin_user_id=actor_id, full_name="Synthetic restricted master", is_active=True)
        session.add(master)
        await session.commit()
        master_id = master.id
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    payload, key = ShopPhysicalCommand(expected_revision=1, evidence="Synthetic NP dispatch"), str(uuid4())
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, payload, "handover", actor_id=actor_id, key=key)
    assert error.value.status_code == 403
    async with database() as session:
        master = await session.get(Master, master_id)
        master.admin_user_id = None
        await session.commit()
    accepted = await execute(database, order_id, payload, "handover", actor_id=actor_id, key=key)
    assert accepted.stage == "dispatched"
    async with database() as session:
        actor = await session.get(AdminUser, actor_id)
        actor.can_manage_shipments = False
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, payload, "handover", actor_id=actor_id, key=key)
    assert error.value.status_code == 403


async def test_cod_dispatch_vs_cancel_has_one_terminal_effect(database, checkout_dependencies, monkeypatch):
    order_id, product_id = await seed(database, kind="nova_poshta", method="cashOnDelivery")
    await attach(database, order_id)
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    barrier = asyncio.Barrier(2)
    async def dispatch():
        await barrier.wait()
        try:
            return await execute(database, order_id, ShopPhysicalCommand(expected_revision=1, evidence="Synthetic dispatch"), "handover")
        except HTTPException as error:
            return error
    async def cancel():
        await barrier.wait()
        async with database() as session:
            try:
                return await OrderService().update_status(session, order_id, OrderStatus.cancelled, admin_user_id=1)
            except HTTPException as error:
                return error
    results = await asyncio.wait_for(asyncio.gather(dispatch(), cancel()), 15)
    assert sum(not isinstance(result, HTTPException) for result in results) == 1
    final = await state(database, order_id)
    if final.order.status == OrderStatus.cancelled:
        assert await stock(database, product_id) == (4, 0)
        assert final.fulfillment is None
    else:
        assert await stock(database, product_id) == (3, 0)
        assert final.fulfillment is not None


async def test_supplier_receipt_vs_handover_preserves_lock_order(database, checkout_dependencies, monkeypatch):
    order_id, product_id = await seed(database, stock=1, quantity=2, backorder=True)
    async with database() as session:
        item = await session.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        item.procurement_status = ProcurementStatus.ordered
        item_id = item.id
        await session.commit()
        service = InventoryService()
        receipt = await service.create_receipt(session, reason="Synthetic receipt", comment=None, idempotency_key=uuid4().hex)
        await service.add_receipt_item(session, receipt.id, product_id=product_id, barcode=None, quantity=1,
            purchase_unit_cost=Decimal("50"), batch_number=None, expiration_date=None,
            allocations=[{"order_item_id": item_id, "quantity": 1}])
        receipt_id = receipt.id
    await execute(database, order_id, manual().model_copy(update={"amount_minor": 20000}), "manual_payment")
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.IsolatedTestFiscalPolicy())
    barrier = asyncio.Barrier(2)
    async def post():
        await barrier.wait()
        async with database() as session:
            return await InventoryService().post_receipt(session, receipt_id, admin_user_id=1, idempotency_key=uuid4().hex)
    async def handover():
        await barrier.wait()
        try:
            return await execute(database, order_id, ShopPhysicalCommand(expected_revision=2, evidence="Synthetic pickup"), "handover")
        except HTTPException as error:
            return error
    received, handed = await asyncio.wait_for(asyncio.gather(post(), handover()), 15)
    assert received.status.value == "posted"
    if isinstance(handed, HTTPException):
        assert handed.detail["code"] == "procurement_required"
        assert await stock(database, product_id) == (2, 2)
    else:
        assert handed.stage == "dispatched"
        assert await stock(database, product_id) == (0, 0)


async def test_direct_sql_cannot_cancel_or_relabel_accepted_real_order(database, checkout_dependencies):
    order_id, _ = await seed(database)
    await execute(database, order_id, manual(), "manual_payment")
    for change in ("simulation=true", "channel='backoffice_pos'", "status='cancelled'", "total_amount=1"):
        async with database() as session:
            with pytest.raises(Exception, match="refund or return"):
                await session.execute(text(f"UPDATE orders SET {change} WHERE id=:order_id"), {"order_id": order_id})
            await session.rollback()
