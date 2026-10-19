"""Stage 1 local checkout contracts against a disposable PostgreSQL schema."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.routes.orders import (
    admin_document, create_preliminary_document, customer_document, guest_document,
)
from app.core.security import create_scoped_token
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderIntent, OrderStatus, PreliminaryDocument
from app.models.product import Product
from app.schemas.order import OrderCreate, PreliminaryDocumentCreate, CustomerOrderResponse
from app.services.order import OrderService
from app.services.order_documents import NOTICE, document_url, operation_key, token_document_id
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies  # noqa: F401

pytestmark = pytest.mark.anyio


def payload(product_id, **overrides):
    data = dict(firstName="Synthetic", lastName="Customer", phoneNumber="+380501112233", items=[{"productId": product_id, "quantity": 2}])
    data.update(overrides)
    return OrderCreate(**data)


async def test_durable_checkout_and_documents(database, checkout_dependencies, monkeypatch):
    """One schema covers concurrency/restart/conflict, snapshots and scoped access."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "shop_checkout_enabled", True)
    async with database() as session:
        product = Product(name="<script>alert(1)</script>", slug="stage-one", price=Decimal("100"), stock_quantity=30, reserved_quantity=0)
        session.add(product)
        await session.commit()
        product_id = product.id
    key = str(uuid4())
    request = payload(product_id, price="0.01", total_amount="0.01")
    calls = 0
    original_prices = __import__("app.services.order", fromlist=["shop_promotion_service"]).shop_promotion_service.price_products

    async def prices(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original_prices(*args, **kwargs)
    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", prices)

    async def checkout():
        async with database() as session:
            return await OrderService().create_order(session, request, idempotency_key=key)
    first, second = await asyncio.wait_for(asyncio.gather(checkout(), checkout()), 15)
    assert first.id == second.id
    assert calls == 1  # Includes promotion validation/usage path only once.
    frozen_response = first._checkout_response
    assert frozen_response == second._checkout_response
    assert first.total_amount == Decimal("200.00")
    assert first.payment_status == "unpaid"
    assert first.channel == "shop"
    assert "external_sync_error" not in frozen_response
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderIntent)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.movement_type == InventoryMovementType.reservation)) == 1
        assert (await session.get(Product, product_id)).reserved_quantity == 2
        document = (await session.execute(select(PreliminaryDocument))).scalar_one()
        document_id = document.id
        original_snapshot = dict(document.snapshot)
        token = document_url(document).rsplit("/", 1)[1]
        assert token_document_id(token) == document_id
        assert document.snapshot["amount_minor"] == 20000
        page = await guest_document(token, session)
        assert NOTICE in page.body.decode()
        assert "&lt;script&gt;" in page.body.decode()
        assert "<script>" not in page.body.decode()
        assert "no-store" == page.headers["cache-control"]
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await OrderService().create_order(session, payload(product_id, comment="different"), idempotency_key=key)
        assert error.value.status_code == 409
    # New service/session simulates restart; replay precedes changed catalogue validation.
    async with database() as session:
        product = await session.get(Product, product_id)
        product.price = Decimal("900.00")
        product.is_active = False
        order = await session.get(Order, first.id)
        order.status = OrderStatus.confirmed
        await session.commit()
    replay = await checkout()
    assert replay._checkout_response == frozen_response
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await customer_document(first.id, document_id, SimpleNamespace(id=999), session)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException):
            await admin_document(first.id + 1, document_id, await session.get(AdminUser, 1), session)
        for invalid in ("invalid", create_scoped_token(document_id, "access", datetime.now(UTC)+timedelta(days=1)), create_scoped_token(document_id, "preliminary_document", datetime.now(UTC)-timedelta(seconds=1))):
            with pytest.raises(HTTPException):
                await guest_document(invalid, session)
    admin_key = str(uuid4())
    async with database() as session:
        newer = await create_preliminary_document(first.id, PreliminaryDocumentCreate(expected_revision=1), admin_key, await session.get(AdminUser, 1), session)
        assert newer.version == 2
    async with database() as session:
        repeated = await create_preliminary_document(first.id, PreliminaryDocumentCreate(expected_revision=1), admin_key, await session.get(AdminUser, 1), session)
        assert newer.id == repeated.id
        documents = (await session.execute(select(PreliminaryDocument).order_by(PreliminaryDocument.version))).scalars().all()
        assert len(documents) == 2 and documents[0].snapshot == original_snapshot
        documents[0].revoked_at = datetime.now(UTC)
        await session.commit()
        with pytest.raises(HTTPException):
            await guest_document(token, session)
    assert (await checkout())._checkout_response == frozen_response

    # Exercise actual promotion redemption limits, without mocked pricing.
    from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
    from app.services.shop_promotion import ShopPromotionService
    from app.models.customer import Customer
    from app.core.database import get_db_session
    from app.api.v1.routes import orders
    from fastapi import FastAPI
    import httpx

    monkeypatch.setattr("app.services.order.shop_promotion_service.price_products", ShopPromotionService().price_products)
    async with database() as session:
        product = await session.get(Product, product_id)
        product.is_active = True
        product.price = Decimal("100.00")
        session.add(ShopPromotion(name="Single use", code="ONCE", trigger=ShopPromotionTrigger.promocode,
            discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal("10"),
            applies_to_all_products=True, usage_limit=1, usage_limit_per_customer=1))
        customer = Customer(phone="+380509998877", name="Synthetic")
        session.add(customer)
        await session.commit()
        customer_id = customer.id
    promo_key = str(uuid4())
    city_ref, warehouse_ref, settlement_ref = str(uuid4()), str(uuid4()), str(uuid4())
    from app.models.nova_poshta import NovaPoshtaSettlement, NovaPoshtaBranch
    async with database() as session:
        session.add(NovaPoshtaSettlement(ref=settlement_ref, city_ref=city_ref, name="Synthetic city",
            search_text="synthetic", delivery_available=True, payload={"area": "Synthetic area"}))
        session.add(NovaPoshtaBranch(ref=warehouse_ref, settlement_ref=settlement_ref, number="12",
            search_text="synthetic branch", payload={"city_ref": city_ref}))
        await session.commit()
    promo_request = payload(product_id, promoCode="ONCE", shippingCompany="novaPost", shippingMethod="warehouse",
        shippingSettlementRef=settlement_ref, shippingCityRef=city_ref, shippingWarehouseRef=warehouse_ref)

    async def promo_checkout():
        async with database() as session:
            return await OrderService().create_order(session, promo_request, idempotency_key=promo_key)
    a, b = await asyncio.wait_for(asyncio.gather(promo_checkout(), promo_checkout()), 15)
    assert a.id == b.id and a.total_amount == Decimal("180.00")
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order).where(Order.promo_code == "ONCE")) == 1
        persisted = await session.get(Order, a.id)
        assert persisted.shipping_city_ref == city_ref and persisted.shipping_warehouse_ref == warehouse_ref
        assert persisted.shipping_payload_json["shippingWarehouseRef"] == warehouse_ref
        with pytest.raises(HTTPException) as exhausted:
            await OrderService().create_order(session, promo_request, idempotency_key=str(uuid4()))
        assert exhausted.value.status_code == 400
        await session.rollback()

    app = FastAPI()
    app.include_router(orders.public_router, prefix="/api/v1/public/orders")
    async def isolated_session():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = isolated_session
    auth_key = str(uuid4())
    valid = create_scoped_token(customer_id, "customer", datetime.now(UTC)+timedelta(days=1))
    expired = create_scoped_token(customer_id, "customer", datetime.now(UTC)-timedelta(seconds=1))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated.test") as client:
        body = payload(product_id).model_dump(mode="json", by_alias=True)
        response = await client.post("/api/v1/public/orders", json=body, headers={"Authorization": "Bearer "+valid, "Idempotency-Key": auth_key})
        assert response.status_code == 201, response.text
        result = response.json()
        retry = await client.post("/api/v1/public/orders", json=body, headers={"Authorization": "Bearer "+expired, "Idempotency-Key": auth_key})
        assert retry.status_code == 401
        recovered = await client.post("/api/v1/public/orders", json=body, headers={"Authorization": "Bearer "+valid, "Idempotency-Key": auth_key})
        assert recovered.status_code == 201 and recovered.json() == result
        assert not {"external_id", "external_sync_status", "external_sync_error"} & result.keys()
        def public_keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(public_keys(child) for child in value.values()))
            if isinstance(value, list):
                return set().union(*(public_keys(child) for child in value))
            return set()
        assert not {"cost", "cost_price", "provider_payload", "secret", "external_sync_error"} & public_keys(result)
        # Missing keys explicitly retain non-idempotent legacy semantics.
        legacy_a = await client.post("/api/v1/public/orders", json=body)
        legacy_b = await client.post("/api/v1/public/orders", json=body)
        assert legacy_a.status_code == legacy_b.status_code == 201
        assert legacy_a.json()["id"] != legacy_b.json()["id"]
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order).where(Order.customer_id == customer_id)) == 1


async def test_key_validation_and_np_snapshot_without_guessing():
    for key in ("1", "customer-phone", "a" * 201):
        with pytest.raises(HTTPException):
            operation_key(key, "guest")
    key = str(uuid4())
    assert operation_key(key, "customer:1") != operation_key(key, "customer:2")
    city_ref, warehouse_ref = str(uuid4()), str(uuid4())
    request = payload(1, shippingCityRef=city_ref, shippingWarehouseRef=warehouse_ref,
        shippingCity="Київ", shippingWarehouseNumber="12", shipping_payload_json={"warehouseLabel": "Відділення 12", "shippingCityRef": "forged", "secret": "hidden"})
    assert request.shipping_payload["shippingCityRef"] == city_ref
    assert request.shipping_payload["shippingWarehouseRef"] == warehouse_ref
    assert request.shipping_payload["warehouseLabel"] == "Відділення 12"
    assert "secret" not in request.shipping_payload
    legacy = Order(customer_name="Synthetic", customer_phone="+380501112233", shipping_city="Київ", shipping_warehouse_number="12", total_amount=Decimal("1"))
    assert legacy.shipping_city_ref is None and legacy.shipping_warehouse_ref is None
    assert legacy.payment_status == "legacy_unknown" and legacy.fiscal_status == "legacy_unknown"
