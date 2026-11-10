"""Authenticated Shop start through the real shared-session FastAPI dependency."""

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.routes.shop_online_payment import public_router
from app.core.database import get_db_session
from app.models.customer import Customer
from app.utils.customer_sessions import issue_customer_token
from tests.test_shop_fiscal_integration import anyio_backend, checkout_dependencies, database, template_database
from tests.test_shop_online_payment_integration import online
from tests.test_shop_payment_resume_integration import create_order
from tests.test_shop_sales_integration import seed

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_authenticated_http_start_keeps_identity_across_rollback(
    database, checkout_dependencies, online, lost_reply,
):
    _, product_id = await seed(database)
    async with database() as session:
        owner = Customer(name="Synthetic owner", phone="+380500001235")
        session.add(owner)
        await session.commit()
        token = issue_customer_token(owner)
    order_id, _ = await create_order(database, product_id, owner=owner)
    online["unknown"] = lost_reply
    app = FastAPI()
    app.include_router(public_router)

    async def db():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/orders/{order_id}/online-payment"
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.post(url, headers={**headers, "Idempotency-Key": str(uuid4())}, json={})
        assert response.status_code == 200
        state = response.json()
        assert state["status"] == ("creation_unknown" if lost_reply else "waiting")
        if lost_reply:
            assert state["payment_status"] == "failed"
            assert state["payment_failure"]["code"] == "invoice_result_unconfirmed"
            assert state["payment_failure"]["reconciliation_required"] is True
            assert state["page_url"] is None
        else:
            assert state["payment_failure"] is None
            assert state["page_url"] == "https://pay.mbnk.biz/syntheticOnlineInvoice"
        replay = await client.post(url, headers={**headers, "Idempotency-Key": str(uuid4())}, json={})
        assert replay.status_code == 200 and replay.json() == state
    assert online["posts"] == 1
