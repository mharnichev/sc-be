"""Shared ingress contracts against an explicitly owned local PostgreSQL test DB."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.routes import customers, orders
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_customer
from app.models.shop_ingress_limits import ShopIngressAdmission
from app.services import shop_ingress_limits as ingress
from app.services.customer_auth import OtpRequestResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def database(monkeypatch):
    url = os.environ.get("SHOP_INGRESS_TEST_DATABASE_URL")
    assert url, "Set SHOP_INGRESS_TEST_DATABASE_URL to a fresh owned local UUID test DB"
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert (parsed.database or "").startswith("sc_shop_r6_ingress_test_")
    schema = "ingress_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engines = [create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
               for _ in range(2)]
    async with engines[0].begin() as connection:
        await connection.run_sync(ShopIngressAdmission.__table__.create)
    factories = [async_sessionmaker(engine, expire_on_commit=False) for engine in engines]
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "shared_ingress_limits_enabled", True)
    monkeypatch.setattr(settings, "otp_source_send_limit", 5)
    monkeypatch.setattr(settings, "otp_source_verify_limit", 20)
    monkeypatch.setattr(settings, "checkout_source_limit", 30)
    monkeypatch.setattr(settings, "source_limit_window_seconds", 60)
    monkeypatch.setattr(settings, "secret_key", "isolated-ingress-stable-test-key")
    monkeypatch.setattr(ingress, "AsyncSessionLocal", factories[0])
    try:
        yield SimpleNamespace(first=factories[0], second=factories[1], schema=schema)
    finally:
        # All test schemas and the owned DB are retained; never touch earlier stands/base.
        for engine in engines:
            await engine.dispose()
        await admin.dispose()


def request(host="203.0.113.12", forwarded=None):
    return Request({"type": "http", "method": "POST", "path": "/", "headers":
                    [(b"x-forwarded-for", forwarded.encode())] if forwarded else [],
                    "client": (host, 12345) if host is not None else None})


async def attempt(factory, req, action="otp_send"):
    try:
        await ingress.admit_shop_ingress(req, action, session_factory=factory)
        return 200
    except HTTPException as error:
        return error.status_code


async def events(database):
    async with database.first() as session:
        return (await session.execute(select(ShopIngressAdmission).order_by(ShopIngressAdmission.id))).scalars().all()


async def test_two_instances_serialize_same_source_without_process_state(database):
    async with database.first() as one, database.second() as two:
        pids = [await one.scalar(select(func.pg_backend_pid())), await two.scalar(select(func.pg_backend_pid()))]
    assert pids[0] != pids[1]
    barrier = asyncio.Barrier(14)

    async def worker(index):
        await barrier.wait()
        return await attempt(database.first if index % 2 else database.second, request())

    outcomes = await asyncio.wait_for(asyncio.gather(*(worker(i) for i in range(14))), timeout=15)
    assert outcomes.count(200) == 5 and outcomes.count(429) == 9
    assert len(await events(database)) == 5
    # Recreating a session factory/client does not reset the shared budget.
    assert await attempt(database.second, request()) == 429
    assert await attempt(database.second, request("203.0.113.13")) == 200


async def test_actions_separate_and_rows_contain_only_hmac_not_source_phone_or_code(database, monkeypatch):
    monkeypatch.setattr(settings, "otp_source_send_limit", 1)
    monkeypatch.setattr(settings, "otp_source_verify_limit", 1)
    monkeypatch.setattr(settings, "checkout_source_limit", 1)
    for action in ("otp_send", "otp_verify", "checkout"):
        assert await attempt(database.first, request(), action) == 200
        assert await attempt(database.second, request(), action) == 429
    rows = await events(database)
    assert len({row.source_key for row in rows}) == 3
    assert {column.name for column in ShopIngressAdmission.__table__.columns} == {"id", "source_key", "action", "admitted_at"}
    for row in rows:
        assert len(row.source_key) == 64 and set(row.source_key) <= set("0123456789abcdef")
        assert row.admitted_at.tzinfo is not None
        assert "203.0.113.12" not in repr(vars(row))


async def test_spoofed_forwarding_headers_and_unknown_source_cannot_bypass(database, monkeypatch):
    monkeypatch.setattr(settings, "otp_source_send_limit", 1)
    assert await attempt(database.first, request(forwarded="198.51.100.1")) == 200
    assert await attempt(database.second, request(forwarded="198.51.100.2")) == 429
    assert await attempt(database.first, request(None, "198.51.100.3")) == 200
    assert await attempt(database.second, request(None, "198.51.100.4")) == 429
    assert len(await events(database)) == 2


async def test_stable_server_hmac_and_clock_expiry_cleanup_only_current_key(database, monkeypatch):
    monkeypatch.setattr(settings, "otp_source_send_limit", 1)
    assert await attempt(database.first, request()) == 200
    assert await attempt(database.first, request("203.0.113.15")) == 200
    initial = await events(database)
    first_key = initial[0].source_key
    async with database.first() as session:
        await session.execute(update(ShopIngressAdmission).values(admitted_at=func.clock_timestamp() - text("interval '61 seconds'")))
        await session.commit()
    assert await attempt(database.second, request()) == 200
    current = await events(database)
    assert len(current) == 2
    assert current[1].source_key == first_key
    # The other inactive digest is intentionally not globally swept.
    assert current[0].id == initial[1].id
    async with database.first() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    assert 0 <= (now - current[1].admitted_at).total_seconds() < 10
    with pytest.raises(HTTPException) as raised:
        await ingress.admit_shop_ingress(request(), "otp_send", session_factory=database.first)
    assert raised.value.status_code == 429
    assert raised.value.detail["code"] == "ingress_limited"
    assert 1 <= int(raised.value.headers["Retry-After"]) <= 60
    assert len(await events(database)) == 2  # Rejections never append events.


async def test_business_rollback_cannot_refund_admitted_budget(database, monkeypatch):
    monkeypatch.setattr(settings, "checkout_source_limit", 1)
    async with database.first() as business:
        await business.execute(text("SELECT 1"))
        await ingress.admit_shop_ingress(request(), "checkout", session_factory=database.second)
        await business.rollback()
    assert await attempt(database.first, request(), "checkout") == 429
    assert len(await events(database)) == 1


@pytest.mark.parametrize("environment", ["production", "staging"])
async def test_shared_limit_forced_in_production_and_staging_even_flagoff(database, monkeypatch, environment):
    monkeypatch.setattr(settings, "app_env", environment)
    monkeypatch.setattr(settings, "shared_ingress_limits_enabled", False)
    monkeypatch.setattr(settings, "otp_source_send_limit", 1)
    assert await attempt(database.first, request()) == 200
    assert await attempt(database.second, request()) == 429


@pytest.mark.parametrize("environment", ["local", "development", "test"])
async def test_explicit_nonproduction_optout_never_opens_store(database, monkeypatch, environment):
    monkeypatch.setattr(settings, "app_env", environment)
    monkeypatch.setattr(settings, "shared_ingress_limits_enabled", False)
    def forbidden():
        raise AssertionError("Opted-out local test must not open a store")
    await ingress.admit_shop_ingress(request(), "otp_send", session_factory=forbidden)
    assert not await events(database)


async def test_store_failure_is_safe_503_and_never_falls_back_local(database):
    def unavailable():
        raise RuntimeError("sensitive source 203.0.113.12 phone/code must not escape")
    for _ in range(2):
        with pytest.raises(HTTPException) as raised:
            await ingress.admit_shop_ingress(request(), "otp_send", session_factory=unavailable)
        assert raised.value.status_code == 503
        assert raised.value.detail == {"code": "ingress_unavailable", "message": "Request admission is temporarily unavailable"}
        assert "sensitive" not in str(raised.value.detail)
    async with database.first() as session:
        await session.execute(text("DROP TABLE shop_ingress_admissions"))
        await session.commit()
    with pytest.raises(HTTPException) as missing:
        await ingress.admit_shop_ingress(request(), "checkout", session_factory=database.first)
    assert missing.value.status_code == 503


@pytest.fixture
def apps(database, monkeypatch):
    monkeypatch.setattr(settings, "shop_checkout_enabled", True)
    async def scoped_admission(req, action):
        await ingress.admit_shop_ingress(req, action, session_factory=req.app.state.ingress_factory)
    monkeypatch.setattr(customers, "admit_shop_ingress", scoped_admission)
    monkeypatch.setattr(orders, "admit_shop_ingress", scoped_admission)
    result = []
    for factory in (database.first, database.second):
        app = FastAPI()
        app.state.ingress_factory = factory
        app.include_router(customers.public_router, prefix="/customers")
        app.include_router(orders.public_router, prefix="/orders")
        async def session_dependency():
            async with database.first() as session:
                yield session
        app.dependency_overrides[get_db_session] = session_dependency
        app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id=1)
        result.append(app)
    return result


async def test_actual_otp_routes_two_instances_different_phones_share_send_budget(database, apps, monkeypatch):
    calls = []
    async def sent(_session, phone):
        calls.append(phone)
        return OtpRequestResult(600, 60, 2, None)
    monkeypatch.setattr(customers.service, "request_otp", sent)
    clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.20", 1000)), base_url="http://local.test") for app in apps]
    try:
        results = await asyncio.gather(*(clients[i % 2].post("/customers/auth/request-otp", json={"phone": f"+38050000{i:04d}"}) for i in range(12)))
        assert sum(response.status_code == 202 for response in results) == 5
        assert sum(response.status_code == 429 for response in results) == 7
        assert len(calls) == len(set(calls)) == 5
        assert len(await events(database)) == 5
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.parametrize("paths,action", [
    (["/customers/auth/request-otp", "/customers/me/phone-change/request-otp"], "otp_send"),
    (["/customers/auth/verify-otp", "/customers/me/phone-change/confirm"], "otp_verify"),
])
async def test_login_and_phone_change_share_action_even_when_business_rejects(database, apps, monkeypatch, paths, action):
    monkeypatch.setattr(settings, "otp_source_send_limit", 1)
    monkeypatch.setattr(settings, "otp_source_verify_limit", 1)
    calls = []
    async def rejected(*args):
        calls.append(True)
        raise HTTPException(400, "Synthetic business rejection")
    for method in ("request_otp", "request_phone_change", "verify_otp", "confirm_phone_change"):
        monkeypatch.setattr(customers.service, method, rejected)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url="http://local.test") as client:
        for index, path in enumerate(paths):
            response = await client.post(path, json={"phone": "+380500001234", "otp_code": "123456"}, headers={"Authorization": "Bearer synthetic-never-parsed"})
            assert response.status_code == (400 if index == 0 else 429)
    assert len(calls) == 1 and (await events(database))[0].action == action


async def test_checkout_uses_shared_budget_and_rejection_has_no_business_side_effect(database, apps, monkeypatch):
    monkeypatch.setattr(settings, "checkout_source_limit", 1)
    calls = []
    async def rejected(*args, **kwargs):
        calls.append(True)
        raise HTTPException(409, "Synthetic insufficient stock")
    monkeypatch.setattr(orders.service, "create_order", rejected)
    body = {"firstName": "Synthetic", "phoneNumber": "+380500001234", "items": [{"productId": 1, "quantity": 1}]}
    for index, app in enumerate(apps):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.30", 1234)), base_url="http://local.test") as client:
            response = await client.post("/orders", json=body, headers={"Idempotency-Key": str(uuid4()), "X-Forwarded-For": f"198.51.100.{index}"})
            assert response.status_code == (409 if index == 0 else 429)
    assert calls == [True] and len(await events(database)) == 1


@pytest.mark.parametrize("path,body", [
    ("/customers/auth/request-otp", {"phone": "+380500001234"}),
    ("/customers/auth/verify-otp", {"phone": "+380500001234", "otp_code": "123456"}),
    ("/customers/me/phone-change/request-otp", {"phone": "+380500001234"}),
    ("/customers/me/phone-change/confirm", {"phone": "+380500001234", "otp_code": "123456"}),
    ("/orders", {"firstName": "Synthetic", "phoneNumber": "+380500001234", "items": [{"productId": 1, "quantity": 1}]}),
])
async def test_unavailable_store_all_entry_routes_stop_before_business(database, apps, monkeypatch, path, body):
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Must not reach business on store failure")
    for method in ("request_otp", "request_phone_change", "verify_otp", "confirm_phone_change"):
        monkeypatch.setattr(customers.service, method, forbidden)
    monkeypatch.setattr(orders.service, "create_order", forbidden)
    def unavailable():
        raise RuntimeError("Synthetic store unavailable")
    apps[0].state.ingress_factory = unavailable
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url="http://local.test") as client:
        response = await client.post(path, json=body, headers={"Authorization": "Bearer synthetic-never-parsed"} if path.startswith('/customers') else {})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ingress_unavailable"
    assert not calls and not await events(database)


@pytest.mark.parametrize("path,action,status_code", [
    ("/customers/me/phone-change/request-otp", "otp_send", 202),
    ("/orders", "checkout", 409),
])
async def test_real_auth_one_slot_pool_admits_before_business_connection(database, monkeypatch, path, action, status_code):
    """No hidden second-connection dependency after real customer authentication."""
    from datetime import UTC, datetime
    from app.models.customer import Customer
    from app.utils.customer_sessions import issue_customer_token

    engine = create_async_engine(os.environ["SHOP_INGRESS_TEST_DATABASE_URL"], pool_size=1,
                                 max_overflow=0, pool_timeout=0.2,
                                 connect_args={"server_settings": {"search_path": database.schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Customer.__table__.create)
    async with factory() as session:
        customer = Customer(phone="+380500001234", phone_verified_at=datetime.now(UTC))
        session.add(customer)
        await session.commit()
        token, customer_id = issue_customer_token(customer), customer.id
    monkeypatch.setattr(ingress, "AsyncSessionLocal", factory)
    monkeypatch.setattr(settings, "shop_checkout_enabled", True)
    calls = []

    async def business(session, *args, **kwargs):
        actor = args[0] if path.startswith('/customers') else kwargs['current_customer']
        assert actor.id == customer_id
        # The same sole connection sees admission already committed before auth.
        assert await session.scalar(select(func.count()).select_from(ShopIngressAdmission).where(
            ShopIngressAdmission.action == action)) == 1
        calls.append(True)
        if status_code == 409:
            raise HTTPException(409, "Synthetic business rejection after authenticated admission")
        return OtpRequestResult(600, 60, 2, None)

    monkeypatch.setattr(customers.service, "request_phone_change", business)
    monkeypatch.setattr(orders.service, "create_order", business)
    app = FastAPI()
    app.include_router(customers.public_router, prefix="/customers")
    app.include_router(orders.public_router, prefix="/orders")
    async def session_dependency():
        async with factory() as session:
            yield session
    app.dependency_overrides[get_db_session] = session_dependency
    body = {"phone": "+380500009999"} if path.startswith('/customers') else {
        "firstName": "Synthetic", "phoneNumber": "+380500001234", "items": [{"productId": 1, "quantity": 1}]}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            response = await client.post(path, json=body, headers={"Authorization": "Bearer " + token})
            assert response.status_code == status_code
            rejected = await client.post(path, json=body, headers={"Authorization": "Bearer invalid-synthetic-token"})
            assert rejected.status_code == 401
        assert calls == [True]
        assert len(await events(database)) == 2  # Invalid auth also spent cheap admission.
    finally:
        await engine.dispose()


async def test_closed_checkout_precedes_admission_store(database, apps, monkeypatch):
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    def unavailable():
        raise AssertionError("Closed checkout must not reach admission store")
    apps[0].state.ingress_factory = unavailable
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url="http://local.test") as client:
        response = await client.post("/orders", json={"firstName": "Synthetic", "phoneNumber": "+380500001234", "items": [{"productId": 1, "quantity": 1}]})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "shop_checkout_closed"
    assert not await events(database)
