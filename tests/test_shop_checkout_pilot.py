"""Closed pilot uses real customer tokens and a one-connection PostgreSQL pool."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.public.router import router
from app.api.v1.routes import orders
from app.core.config import Settings, settings
from app.core.database import Base, get_db_session
from app.models.customer import Customer
from app.models.order import Order, OrderIntent
from app.models.product import Product
from app.models.shop_ingress_limits import ShopIngressAdmission
from app.services import shop_ingress_limits as ingress
from app.utils.customer_sessions import issue_customer_token


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("value", ["*", "10,", "10,,11", " 10", "10 11", "0", "-10", "1.0", "[10]", "true", "10,abc", "01", "\u0661\u0660", "10," * 101])
def test_pilot_config_rejects_malformed_ids(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SHOP_CHECKOUT_PILOT_CUSTOMER_IDS=value)


def test_pilot_config_defaults_and_public_conflict():
    config = Settings(_env_file=None)
    assert not config.shop_checkout_enabled and not config.shop_checkout_pilot_enabled
    assert config.checkout_pilot_customer_ids == frozenset()
    assert Settings(_env_file=None, SHOP_CHECKOUT_PILOT_CUSTOMER_IDS="10,11").checkout_pilot_customer_ids == {10, 11}
    with pytest.raises(ValidationError, match="SHOP_CHECKOUT_ENABLED=false"):
        Settings(_env_file=None, SHOP_CHECKOUT_ENABLED=True, SHOP_CHECKOUT_PILOT_ENABLED=True)


@pytest.fixture
async def pilot(monkeypatch):
    url = os.environ.get("INVENTORY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires fresh local disposable test PostgreSQL")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "::1", "localhost"} and "_test_" in parsed.database
    schema = "pilot_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=0.2,
        connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "10")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "shared_ingress_limits_enabled", False)
    monkeypatch.setattr(ingress, "AsyncSessionLocal", factory)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/public")

    async def session_dependency():
        async with factory() as session:
            yield session
    app.dependency_overrides[get_db_session] = session_dependency
    async with factory() as session:
        customers = [Customer(id=i, name="Synthetic", phone=f"+3805000000{i}", is_active=True,
                              phone_verified_at=datetime.now(UTC)) for i in (10, 11)]
        product = Product(name="Synthetic pilot", slug="pilot", price=Decimal("100"),
                          stock_quantity=5, reserved_quantity=0, allow_backorder=False)
        session.add_all([*customers, product])
        await session.commit()
        tokens = [issue_customer_token(customer) for customer in customers]
        body = {"firstName": "Synthetic", "phoneNumber": customers[0].phone,
                "items": [{"productId": product.id, "quantity": 1}]}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pilot.test", timeout=5) as client:
            yield client, factory, body, tokens
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("identity", ["guest", "other", "invalid", "basic", "expired", "admin", "forged", "revoked", "unverified", "inactive"])
async def test_direct_api_pilot_denies_before_order_service(pilot, monkeypatch, identity):
    client, factory, body, tokens = pilot
    token = tokens[0]
    if identity in {"unverified", "inactive", "revoked"}:
        async with factory() as session:
            customer = await session.get(Customer, 10)
            if identity == "unverified":
                customer.phone_verified_at = None
            elif identity == "inactive":
                customer.is_active = False
            else:
                customer.session_version += 1
            await session.commit()
    if identity in {"expired", "admin", "forged"}:
        token = jwt.encode({"sub": "10", "scope": "admin" if identity == "admin" else "customer",
                            "exp": datetime.now(UTC) + timedelta(seconds=-10 if identity == "expired" else 60)},
                           "forged-test-secret" if identity == "forged" else settings.secret_key, algorithm="HS256")
    authorization = {
        "guest": None, "other": "Bearer " + tokens[1], "invalid": "Bearer invalid",
        "basic": "Basic " + token,
    }.get(identity, "Bearer " + token)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Denied pilot must never reach OrderService")
    monkeypatch.setattr(orders.service, "create_order", forbidden)
    response = await client.post("/api/v1/public/orders?SHOP_CHECKOUT_ENABLED=true&customer_id=10",
        json={**body, "customer_id": 10, "shop_checkout_pilot_customer_ids": "10"},
        headers={"Idempotency-Key": str(uuid4()), "X-Customer-ID": "10", "X-Shop-Checkout-Enabled": "true",
                 **({"Authorization": authorization} if authorization else {})})
    assert response.status_code == (401 if identity in {"invalid", "basic", "expired", "admin", "forged", "revoked"} else 503), response.text
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(OrderIntent)) == 0
        assert (await session.scalar(select(Product))).reserved_quantity == 0
        assert await session.scalar(select(func.count()).select_from(ShopIngressAdmission)) == 1


@pytest.mark.anyio
async def test_verified_allowlisted_pilot_accepts_and_replays_with_public_false_pool_one(pilot):
    client, factory, body, tokens = pilot
    headers = {"Authorization": "Bearer " + tokens[0], "Idempotency-Key": str(uuid4())}
    first = await client.post("/api/v1/public/orders", json=body, headers=headers)
    assert first.status_code == 201, first.text
    replay = await client.post("/api/v1/public/orders", json=body, headers=headers)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert not settings.shop_checkout_enabled
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert (await session.scalar(select(Product))).reserved_quantity == 1
        assert await session.scalar(select(func.count()).select_from(ShopIngressAdmission)) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("enabled,ids", [(False, "10"), (True, ""), (True, "10,bad")])
async def test_empty_disabled_or_invalid_pilot_is_closed_before_ingress(pilot, monkeypatch, enabled, ids):
    client, factory, body, tokens = pilot
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", enabled)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", ids)
    response = await client.post("/api/v1/public/orders", json=body, headers={"Authorization": "Bearer " + tokens[0]})
    assert response.status_code == 503
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ShopIngressAdmission)) == 0


@pytest.mark.anyio
async def test_pilot_source_budget_and_store_failure_precede_auth(pilot, monkeypatch):
    client, factory, body, tokens = pilot
    monkeypatch.setattr(settings, "checkout_source_limit", 1)
    headers = {"Authorization": "Bearer forged"}
    assert (await client.post("/api/v1/public/orders", json=body, headers=headers)).status_code == 401
    response = await client.post("/api/v1/public/orders", json=body, headers=headers)
    assert response.status_code == 429 and response.json()["detail"]["code"] == "ingress_limited"
    def unavailable():
        raise RuntimeError("Synthetic store unavailable")
    monkeypatch.setattr(ingress, "AsyncSessionLocal", unavailable)
    response = await client.post("/api/v1/public/orders", json=body, headers=headers)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "ingress_unavailable"
