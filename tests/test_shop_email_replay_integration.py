"""Reject new display-name email input without invalidating accepted legacy intents."""
import pytest

from app.schemas.order import OrderCreate
from app.services.order import OrderService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_shop_checkout_http_integration import (
    body, checkout_app, checkout_client, headers, local_network_only, persisted_state, seed,
)


@pytest.mark.anyio
async def test_display_name_rejection_and_exact_legacy_http_replay_preserve_db_snapshot(checkout_client, database):
    product_id, _ = await seed(database, stock=1)
    raw = body(product_id, email='Alice <other@example.org>')
    request_headers = headers()
    # Seed a genuine accepted legacy order through the unchanged backoffice/service contract.
    legacy = OrderCreate.model_validate(raw)
    assert legacy.email == 'other@example.org'
    async with database() as session:
        order = await OrderService().create_order(session, legacy,
            idempotency_key=request_headers['Idempotency-Key'])
        accepted = order._checkout_response
    before = await persisted_state(database)
    rejected = await checkout_client.post('/orders', json=raw, headers=headers())
    assert rejected.status_code == 422, rejected.text
    assert await persisted_state(database) == before
    replay = await checkout_client.post('/orders', json=raw, headers=request_headers)
    assert replay.status_code == 201, replay.text
    assert replay.json() == accepted
    assert await persisted_state(database) == before
