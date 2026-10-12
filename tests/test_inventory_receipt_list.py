from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1.routes.inventory import backoffice_router, list_receipts
from app.dependencies.auth import get_current_admin_user
from app.services.inventory import InventoryService
from test_inventory_integration import database  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_receipts_include_drafts_and_posted_with_stable_pagination():
    documents = [SimpleNamespace(id=21, status="draft"), SimpleNamespace(id=20, status="posted")]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: documents))
    session = SimpleNamespace(scalar=AsyncMock(return_value=21), execute=AsyncMock(return_value=result))
    items, total = await InventoryService().list_receipts(session, page=2, page_size=20)
    assert items == documents
    assert total == 21
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "inventory_receipts.created_at DESC, inventory_receipts.id DESC" in str(compiled)
    assert "WHERE" not in str(compiled)
    assert compiled.params["param_1"] == 20
    assert compiled.params["param_2"] == 20
    paths = [str(option.path) for option in statement._with_options]
    assert any("InventoryReceiptItem.allocations" in path for path in paths)
    assert any("InventoryReceiptItem.product" in path for path in paths)  # barcode reads the product relationship.


@pytest.mark.anyio
async def test_empty_receipt_list_and_route_contract(monkeypatch):
    service = AsyncMock(return_value=([], 0))
    monkeypatch.setattr("app.api.v1.routes.inventory.inventory_service.list_receipts", service)
    session = object()
    response = await list_receipts(SimpleNamespace(page=3, page_size=20), session)
    assert response.model_dump() == {"total": 0, "page": 3, "page_size": 20, "items": []}
    service.assert_awaited_once_with(session, page=3, page_size=20)
    route = next(route for route in backoffice_router.routes if route.path == "/receipts" and "GET" in route.methods)
    assert get_current_admin_user in [dependency.call for dependency in route.dependant.dependencies]


@pytest.mark.anyio
async def test_receipt_http_list_serializes_real_items_and_enforces_auth(database):
    from decimal import Decimal
    from httpx import ASGITransport, AsyncClient
    from app.core.database import get_db_session
    from app.core.security import create_access_token
    from app.main import app
    from app.models.product import Product

    async with database() as session:
        product = Product(name='Receipt item', slug='receipt-item', price=Decimal('100.00'),
                          barcode='4820000000001', stock_quantity=0, reserved_quantity=0)
        session.add(product)
        await session.commit()
        service = InventoryService()
        receipt = await service.create_receipt(session, reason='Test shipment', comment=None,
                                               idempotency_key='receipt-list-draft')
        await service.add_receipt_item(session, receipt.id, product_id=product.id, barcode=None,
            quantity=2, purchase_unit_cost=Decimal('50.00'), batch_number=None,
            expiration_date=None, allocations=[])
        identifier = receipt.id

    async def dependency():
        async with database() as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db_session] = dependency
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://receipt.test') as client:
            path = '/api/v1/backoffice/inventory/receipts'
            assert (await client.get(path)).status_code == 401
            headers = {'Authorization': 'Bearer ' + create_access_token('1')}
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload['total'] == 1 and len(payload['items']) == 1
            assert payload['items'][0]['id'] == identifier
            assert payload['items'][0]['status'] == 'draft'
            assert payload['items'][0]['items'][0]['barcode'] == product.barcode
            assert payload['items'][0]['items'][0]['allocations'] == []
            empty = await client.get(path, params={'page': 2}, headers=headers)
            assert empty.status_code == 200 and empty.json()['items'] == []
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
