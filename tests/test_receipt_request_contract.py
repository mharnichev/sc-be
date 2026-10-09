"""HTTP receipt scans require the same bounded retry identity as other operations."""
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.routes.inventory import backoffice_router
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.services.inventory import inventory_service


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_receipt_scan_requires_bounded_header_and_forwards_exact_request_identity(monkeypatch):
    app = FastAPI()
    app.include_router(backoffice_router, prefix="/inventory")
    session = object()
    async def db():
        yield session
    async def admin():
        return SimpleNamespace(id=1)
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = admin
    calls = []
    async def add_item(actual_session, receipt_id, **payload):
        calls.append((actual_session, receipt_id, payload))
        now = datetime(2026, 10, 9, tzinfo=UTC)
        return dict(id=receipt_id, reason="Supplier delivery", status="draft", created_at=now, updated_at=now, items=[])
    monkeypatch.setattr(inventory_service, "add_receipt_item", add_item)
    body = dict(product_id=3, quantity=2, purchase_unit_cost="50.00", batch_number="LOT-A")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post("/inventory/receipts/7/items", json=body)
        assert missing.status_code == 422
        assert any(error["loc"] == ["header", "Idempotency-Key"] for error in missing.json()["detail"])
        too_long = await client.post("/inventory/receipts/7/items", json=body, headers={"Idempotency-Key": "x" * 129})
        assert too_long.status_code == 422
        assert calls == []
        key = "44afcfd1-9a2a-485e-95b4-cce58e1f8e10"
        accepted = await client.post("/inventory/receipts/7/items", json=body, headers={"Idempotency-Key": key})
        assert accepted.status_code == 200
        assert calls[0][0] is session
        assert calls[0][1] == 7
        assert calls[0][2]["idempotency_key"] == key
        assert calls[0][2]["quantity"] == 2
