"""Isolated local POS safety, concurrency and inventory contracts."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderPayment, OrderHandover, OrderStatus
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.product import Product
from app.schemas.order import POSOrderCreate, OrderPaymentCreate, OrderHandoverCreate, PublicOrderResponse
from app.services import local_sales
from app.services.order import OrderService
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product, checkout_payload

pytestmark = pytest.mark.anyio


async def test_simulated_lifecycle_is_durable_and_exactly_once(database, checkout_dependencies, monkeypatch):
    monkeypatch.setattr(settings, "local_sales_simulation_enabled", True)
    monkeypatch.setattr(settings, "app_env", "test")
    product = await add_product(database, stock=1)
    async def prepare(key):
        async with database() as session:
            return await local_sales.prepare(session, await session.get(AdminUser, 1), POSOrderCreate(items=[{"product_id": product.id, "quantity": 1}], simulation=True), key)
    key = str(uuid4())
    a, b = await asyncio.gather(prepare(key), prepare(key))
    assert a.model_dump() == b.model_dump()
    assert a.customer_phone is None and a.total_amount == 100
    assert a.sales_actions["can_record_payment"]
    payment = OrderPaymentCreate(expected_revision=1, amount_minor=10000, method="cash", source="operator_confirmation", evidence="Fixture cash")
    async def pay(key, request=payment):
        async with database() as session:
            return await local_sales.command(session, await session.get(AdminUser, 1), a.id, request, key)
    with pytest.raises(HTTPException) as err:
        await pay(str(uuid4()), payment.model_copy(update={"amount_minor": 1}))
    assert err.value.detail["code"] == "amount_mismatch"
    with pytest.raises(HTTPException) as err:
        await pay(str(uuid4()), payment.model_copy(update={"expected_revision": 99}))
    assert err.value.detail["code"] == "revision_conflict"
    pay_key = str(uuid4())
    c, d = await asyncio.gather(pay(pay_key), pay(pay_key))
    assert c.model_dump() == d.model_dump()
    assert c.payment_status == "simulated_paid" and c.status != OrderStatus.paid
    assert c.payments[0].amount_minor == 10000 and c.payments[0].operator_id == 1
    with pytest.raises(HTTPException) as err:
        await pay(str(uuid4()), payment.model_copy(update={"expected_revision": 2}))
    assert err.value.detail["code"] == "payment_exists"
    async with database() as session:
        revoked = SimpleNamespace(id=1, is_active=True, is_superuser=False, can_record_sales=False, can_post_inventory=True)
        with pytest.raises(HTTPException) as err:
            await local_sales.command(session, revoked, a.id, payment, pay_key)
        assert err.value.status_code == 403
    handover = OrderHandoverCreate(expected_revision=2, evidence="Fixture handover")
    handover_key = str(uuid4())
    async def hand():
        async with database() as session:
            return await local_sales.command(session, await session.get(AdminUser, 1), a.id, handover, handover_key, handover=True)
    e, f = await asyncio.gather(hand(), hand())
    assert e.model_dump() == f.model_dump()
    assert e.fulfillment_status == "simulated_handed_over"
    async with database() as session:
        with pytest.raises(HTTPException) as err:
            await local_sales.command(session, await session.get(AdminUser, 1), a.id,
                handover.model_copy(update={"expected_revision": e.revision}), str(uuid4()), handover=True)
        assert err.value.detail["code"] == "terminal_order"
    assert (await pay(pay_key)).model_dump() == c.model_dump()  # Original response after later command.
    async with database() as session:
        order = await local_sales.load(session, a.id)
        public = PublicOrderResponse.model_validate(order).model_dump()
        assert "payments" not in public and "operator_id" not in str(public)
        assert order.preliminary_documents[0].snapshot["simulation"] is True
        item = await session.get(Product, product.id)
        assert (item.stock_quantity, item.reserved_quantity) == (0, 0)
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.movement_type == InventoryMovementType.shipment)) == 1
        with pytest.raises(HTTPException) as err:
            await OrderService().update_status(session, a.id, OrderStatus.cancelled, admin_user_id=1)
        assert err.value.status_code == 409
    with pytest.raises(HTTPException) as err:
        await pay(pay_key, payment.model_copy(update={"amount_minor": 1}))
    assert err.value.detail["code"] == "idempotency_conflict"


async def test_local_only_guards_permissions_money_and_legacy_patch(database, checkout_dependencies, monkeypatch):
    product = await add_product(database, stock=5)
    monkeypatch.setattr(settings, "local_sales_simulation_enabled", False)
    payload = POSOrderCreate(items=[{"product_id": product.id, "quantity": 1}])
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        prepared = await local_sales.prepare(session, actor, payload, str(uuid4()))
        assert not prepared.simulation and not prepared.sales_actions["can_record_payment"]
    async with database() as session:
        with pytest.raises(HTTPException) as err:
            await local_sales.prepare(session, await session.get(AdminUser, 1), payload.model_copy(update={"simulation": True}), str(uuid4()))
        assert err.value.detail["code"] == "simulation_disabled"
    for op in (OrderStatus.paid, OrderStatus.completed):
        async with database() as session:
            with pytest.raises(HTTPException) as err:
                await OrderService().update_status(session, prepared.id, op, admin_user_id=1)
            assert err.value.detail["code"] == "profile_command_required"
    monkeypatch.setattr(settings, "local_sales_simulation_enabled", True)
    async with database() as session:
        with pytest.raises(HTTPException) as err:
            await local_sales.command(session, await session.get(AdminUser, 1), prepared.id,
                OrderPaymentCreate(expected_revision=1, amount_minor=10000, method="card", source="operator_confirmation", evidence="fixture"), str(uuid4()))
        assert err.value.detail["code"] == "local_only"  # Enabling test mode cannot transform real order.
    async with database() as session:
        actor = SimpleNamespace(id=1, is_active=True, is_superuser=False, can_record_sales=False, can_post_inventory=True)
        with pytest.raises(HTTPException) as err:
            await local_sales.prepare(session, actor, payload, str(uuid4()))
        assert err.value.status_code == 403


@pytest.mark.parametrize("competitor", ["pos", "shop"])
async def test_shared_last_unit_reservation(database, checkout_dependencies, competitor):
    product = await add_product(database, stock=1)
    async def pos():
        async with database() as session:
            try:
                return await local_sales.prepare(session, await session.get(AdminUser, 1), POSOrderCreate(items=[{"product_id": product.id, "quantity": 1}]), str(uuid4()))
            except HTTPException as e:
                return e.status_code
    async def other():
        if competitor == "pos":
            return await pos()
        async with database() as session:
            try:
                return await OrderService().create_order(session, checkout_payload(product.id))
            except HTTPException as e:
                return e.status_code
    results = await asyncio.gather(pos(), other())
    assert sum(result == 409 for result in results) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        item = await session.get(Product, product.id)
        assert (item.stock_quantity, item.reserved_quantity) == (1, 1)


async def test_actual_routes_require_auth_permissions_key_and_strict_money(database, checkout_dependencies, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.orders import backoffice_router
    from app.dependencies.auth import get_current_admin_user
    from app.core.database import get_db_session
    application = FastAPI()
    application.include_router(backoffice_router, prefix="/orders")
    async def sessions():
        async with database() as session:
            yield session
    application.dependency_overrides[get_db_session] = sessions
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://isolated.test") as client:
        assert (await client.get("/orders/pos-capabilities")).status_code == 401
        actor = SimpleNamespace(id=1, is_active=True, is_superuser=False, can_record_sales=False, can_post_inventory=True)
        application.dependency_overrides[get_current_admin_user] = lambda: actor
        body = {"items": [{"product_id": 999, "quantity": 1}]}
        assert (await client.post("/orders", json=body, headers={"Idempotency-Key": str(uuid4())})).status_code == 403
        actor.can_record_sales = True
        assert (await client.post("/orders", json=body)).status_code == 422
        assert (await client.post("/orders", json=body, headers={"Idempotency-Key": "predictable"})).status_code == 422
        for amount in (True, 1.5, "100"):
            result = await client.post("/orders/999/payments", json={"expected_revision": 1,
                "amount_minor": amount, "method": "cash", "source": "operator_confirmation", "evidence": "fixture"},
                headers={"Idempotency-Key": str(uuid4())})
            assert result.status_code == 422


async def test_large_kopeck_payment_fits_order_numeric_range(database, checkout_dependencies, monkeypatch):
    from decimal import Decimal
    monkeypatch.setattr(settings, "local_sales_simulation_enabled", True)
    monkeypatch.setattr(settings, "app_env", "test")
    async def large_prices(_session, products, **_kwargs):
        return {item.id: SimpleNamespace(base_price=Decimal("22000000.00"), price=Decimal("22000000.00"),
            discount_amount=Decimal("0.00"), promotion_id=None, promotion_name=None, promotion_code=None) for item in products}
    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", large_prices)
    product = await add_product(database, stock=1)
    async with database() as session:
        order = await local_sales.prepare(session, await session.get(AdminUser, 1),
            POSOrderCreate(items=[{"product_id": product.id, "quantity": 1}], simulation=True), str(uuid4()))
    async with database() as session:
        result = await local_sales.command(session, await session.get(AdminUser, 1), order.id,
            OrderPaymentCreate(expected_revision=1, amount_minor=2200000000, method="card",
                source="operator_confirmation", evidence="Large synthetic payment"), str(uuid4()))
        assert result.payments[0].amount_minor == 2200000000
        assert result.payments[0].currency == "UAH"
    async with database() as session:
        fact = (await session.execute(select(OrderPayment))).scalar_one()
        assert fact.amount_minor == 2200000000


@pytest.mark.parametrize("simulation", [False, True])
@pytest.mark.parametrize("discount_type", ["percent", "fixed_amount"])
async def test_zero_total_pos_rejects_and_rolls_back_entire_intent(
    database, checkout_dependencies, monkeypatch, simulation, discount_type,
):
    """Real promotion pricing must not persist a POS order that cannot be paid."""
    from decimal import Decimal
    from app.models.order import OrderIntent, PreliminaryDocument
    from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
    from app.services.shop_promotion import ShopPromotionService

    monkeypatch.setattr(settings, "local_sales_simulation_enabled", True)
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", ShopPromotionService().price_products)
    product = await add_product(database, stock=1)
    async with database() as session:
        promotion = ShopPromotion(name="Synthetic free item", code="FREE", trigger=ShopPromotionTrigger.promocode,
            discount_type=ShopPromotionDiscountType(discount_type), discount_value=Decimal("100"),
            applies_to_all_products=True, usage_limit=1)
        session.add(promotion)
        await session.commit()
        promotion_id = promotion.id

    key = str(uuid4())
    payload = POSOrderCreate(items=[{"product_id": product.id, "quantity": 1}],
        promo_code="FREE", simulation=simulation)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await local_sales.prepare(session, await session.get(AdminUser, 1), payload, key)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "unsupported_zero_total_pos"
        # Inspect before closing the failed session: rejection must explicitly
        # roll back the order, document, reservation, and idempotency claim.
        for model in (Order, OrderIntent, PreliminaryDocument, InventoryMovement, OrderPayment, OrderHandover):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        stored = await session.get(Product, product.id)
        assert (stored.stock_quantity, stored.reserved_quantity) == (1, 0)

    # The rejected order must not consume the limited promotion or its key.
    # Correcting the server promotion allows that exact POS intent to succeed.
    async with database() as session:
        promotion = await session.get(ShopPromotion, promotion_id)
        promotion.discount_value = Decimal("50")
        await session.commit()
    async with database() as session:
        prepared = await local_sales.prepare(session, await session.get(AdminUser, 1), payload, key)
        assert prepared.total_amount == Decimal("50.00")
        assert prepared.simulation is simulation
    async with database() as session:
        replay = await local_sales.prepare(session, await session.get(AdminUser, 1), payload, key)
        assert replay.model_dump() == prepared.model_dump()
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderIntent)) == 1
        stored = await session.get(Product, product.id)
        assert (stored.stock_quantity, stored.reserved_quantity) == (1, 1)
