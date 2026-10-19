"""Closed public checkout is rejected before authentication or database access."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.routes import orders
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_optional_current_customer


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic invalid", "Bearer"])
@pytest.mark.parametrize("key", [None, str(uuid4()), "invalid-key"])
async def test_closed_checkout_precedes_auth_and_database(monkeypatch, authorization, key):
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    app = FastAPI()
    app.include_router(orders.public_router, prefix="/orders")

    async def forbidden_dependency():
        raise AssertionError("Closed checkout must not resolve authentication or database dependencies")

    async def forbidden_checkout(*args, **kwargs):
        raise AssertionError("Closed checkout must not reach order creation")

    app.dependency_overrides[get_db_session] = forbidden_dependency
    app.dependency_overrides[get_optional_current_customer] = forbidden_dependency
    monkeypatch.setattr(orders.service, "create_order", forbidden_checkout)
    request_headers = {"X-Shop-Checkout-Enabled": "true"}
    if authorization is not None:
        request_headers["Authorization"] = authorization
    if key is not None:
        request_headers["Idempotency-Key"] = key
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://closed.test") as client:
        response = await client.post("/orders?SHOP_CHECKOUT_ENABLED=true&shop_checkout_enabled=true", json={
            "firstName": "Synthetic", "phoneNumber": "+380500001234",
            "items": [{"productId": 1, "quantity": 1}],
            "SHOP_CHECKOUT_ENABLED": True, "shop_checkout_enabled": True,
        }, headers=request_headers)
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": {
        "code": "shop_checkout_closed", "message": "Shop checkout is temporarily unavailable",
    }}
