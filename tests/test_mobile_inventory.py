from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.v1.routes.inventory import backoffice_router
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_inventory_poster
from app.models.product import Product
from app.models.brand import Brand
from app.schemas.inventory import InventoryProductCreateRequest, InventoryProductResponse
from app.services.inventory import InventoryService, inventory_service, normalize_barcode


def card(**overrides):
    data = dict(id=7, name="Pomade", slug="pomade", price=0, stock_quantity=5,
                reserved_quantity=2, allow_backorder=False, brand=None, barcode=None)
    data.update(overrides)
    return Product(**data)


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


class Session:
    def __init__(self, results, error=None):
        self.results = list(results)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.added = []
        self.error = error

    async def execute(self, statement):
        self.statements.append(statement)
        return Result(self.results.pop(0))

    async def get(self, model, key):
        return None

    def add(self, value):
        value.id = 7
        self.added.append(value)

    async def commit(self):
        self.commits += 1
        if self.error:
            raise self.error

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize("code", ["00001234", "Ab-c", " 00 ab "])
def test_scanner_text_is_preserved(code):
    assert normalize_barcode(code) == code


@pytest.mark.anyio
async def test_exact_lookup_and_unknown_code():
    product = card(barcode="000123")
    session = Session([product, None])
    assert await InventoryService().get_product_by_barcode(session, "000123") is product
    assert list(session.statements[0].compile().params.values()) == ["000123"]
    with pytest.raises(HTTPException) as error:
        await InventoryService().get_product_by_barcode(session, "123")
    assert error.value.status_code == 404


@pytest.mark.anyio
async def test_bind_is_audited_without_changing_stock_and_is_replay_safe():
    product = card()
    session = Session([product, None, product, product, product])
    service = InventoryService()
    await service.bind_barcode(session, 7, "000123", 11)
    await service.bind_barcode(session, 7, "000123", 12)
    assert product.barcode == "000123"
    assert product.barcode_bound_by_user_id == 11
    assert (product.stock_quantity, product.reserved_quantity) == (5, 2)
    assert session.commits == 1
    assert session.statements[0]._for_update_arg is not None


@pytest.mark.anyio
@pytest.mark.parametrize("existing,duplicate", [("other", None), (None, 12)])
async def test_bind_conflicts(existing, duplicate):
    session = Session([card(barcode=existing), duplicate])
    with pytest.raises(HTTPException) as error:
        await InventoryService().bind_barcode(session, 7, "000123", 11)
    assert error.value.status_code == 409
    assert session.commits == 0


@pytest.mark.anyio
async def test_concurrent_barcode_unique_constraint_becomes_conflict():
    session = Session([card(), None], IntegrityError("insert", {}, Exception("unique")))
    with pytest.raises(HTTPException) as error:
        await InventoryService().bind_barcode(session, 7, "000123", 11)
    assert error.value.status_code == 409
    assert session.rollbacks == 1


@pytest.mark.anyio
async def test_create_card_zero_stock_and_idempotent_replay():
    data = InventoryProductCreateRequest(name="Pomade", barcode="000123").model_dump(mode="json")
    product = card(barcode="000123", created_by_user_id=11, creation_payload=data)
    session = Session([None, product, product, product])
    service = InventoryService()
    await service.create_product_card(session, data=data, admin_user_id=11, idempotency_key="create-1")
    created = session.added[0]
    assert (created.stock_quantity, created.reserved_quantity) == (0, 0)
    assert created.is_active is False
    assert created.created_by_user_id == created.barcode_bound_by_user_id == 11
    await service.create_product_card(session, data=data, admin_user_id=11, idempotency_key="create-1")
    assert session.commits == 1
    with pytest.raises(HTTPException) as error:
        await service.create_product_card(Session([product]), data={**data, "name": "Changed"}, admin_user_id=11, idempotency_key="create-1")
    assert error.value.status_code == 409


@pytest.mark.anyio
async def test_inventory_post_permission(monkeypatch):
    async def non_master(*args):
        return False
    monkeypatch.setattr("app.services.sales_access.restricted_master", non_master)
    with pytest.raises(HTTPException) as error:
        await get_inventory_poster(SimpleNamespace(is_superuser=False, can_post_inventory=False))
    assert error.value.status_code == 403
    for user in [SimpleNamespace(is_superuser=True, can_post_inventory=False), SimpleNamespace(is_superuser=False, can_post_inventory=True)]:
        assert await get_inventory_poster(user) is user


def test_http_master_can_scan_bind_and_create_but_cannot_post(monkeypatch):
    app = FastAPI()
    app.include_router(backoffice_router, prefix="/inventory")
    user = SimpleNamespace(id=11, is_superuser=False, can_post_inventory=False)
    app.dependency_overrides[get_current_admin_user] = lambda: user
    app.dependency_overrides[get_db_session] = lambda: None
    product = card(barcode="000123")

    async def lookup(*args, **kwargs):
        return product

    async def bind(*args, **kwargs):
        return product

    async def create(*args, **kwargs):
        return product

    monkeypatch.setattr(inventory_service, "get_product_by_barcode", lookup)
    monkeypatch.setattr(inventory_service, "bind_barcode", bind)
    monkeypatch.setattr(inventory_service, "create_product_card", create)
    with TestClient(app) as client:
        assert client.get("/inventory/products/barcode/000123").json()["barcode"] == "000123"
        assert client.put("/inventory/products/7/barcode", json={"barcode": "000123"}).status_code == 200
        assert client.post("/inventory/products", json={"name": "Pomade", "barcode": "000123"}, headers={"Idempotency-Key": "create-1"}).status_code == 201
        assert client.post("/inventory/receipts/7/post", headers={"Idempotency-Key": "post-1"}).status_code == 403
        assert client.patch("/inventory/products/7/settings", json={"barcode": "000123", "allow_backorder": False}).status_code == 403
        assert client.post("/inventory/counts", json={"reason": "Count"}, headers={"Idempotency-Key": "count-1"}).status_code == 403
        assert client.patch("/inventory/users/11/permissions", json={"can_post_inventory": True}).status_code == 403
        assert client.put("/inventory/products/7/barcode", json={"barcode": 123}).status_code == 422
        assert client.put("/inventory/products/7/barcode", json={"barcode": "   "}).status_code == 422


def test_rich_selection_response():
    product = card(brand=Brand(name="Brand", slug="brand"), brand_id=2, model_name="Clay", package_size="100 ml", volume_ml=100, image_url="/photo.jpg")
    response = InventoryProductResponse.model_validate(product)
    assert response.brand_name == "Brand"
    assert response.model_name == "Clay"
    assert response.available == 3


@pytest.mark.anyio
async def test_search_by_name_or_sku_is_paginated():
    product = card(sku="SKU-1")

    class ListResult:
        def scalars(self):
            return self

        def all(self):
            return [product]

    class SearchSession:
        async def scalar(self, statement):
            self.count_statement = statement
            return 23

        async def execute(self, statement):
            self.statement = statement
            return ListResult()

    session = SearchSession()
    products, total = await InventoryService().list_stock(session, page=2, page_size=10, search="SKU-1")
    sql = str(session.statement.compile())
    params = session.statement.compile().params
    assert products == [product] and total == 23
    assert "products.name" in sql and "products.sku" in sql
    assert "%SKU-1%" in params.values()
    assert session.statement._offset_clause.value == 10
    assert session.statement._limit_clause.value == 10


def test_http_requires_auth_and_poster_can_post(monkeypatch):
    app = FastAPI()
    app.include_router(backoffice_router, prefix='/inventory')
    app.dependency_overrides[get_db_session] = lambda: None
    with TestClient(app) as client:
        assert client.get('/inventory/stock').status_code == 401
    user = SimpleNamespace(id=1, is_superuser=True, can_post_inventory=False)
    app.dependency_overrides[get_current_admin_user] = lambda: user
    calls = []
    async def post(session, receipt_id, admin_user_id, key):
        from datetime import UTC, datetime
        calls.append((receipt_id, admin_user_id, key))
        now = datetime.now(UTC)
        return dict(id=receipt_id, reason='Delivery', status='posted', posted_by_user_id=admin_user_id,
                    posted_at=now, created_at=now, updated_at=now, items=[])
    monkeypatch.setattr(inventory_service, 'post_receipt', post)
    with TestClient(app) as client:
        response = client.post('/inventory/receipts/3/post', headers={'Idempotency-Key': 'post-3'})
        assert response.status_code == 200
        assert response.json()['posted_by_user_id'] == 1
        assert calls == [(3, 1, 'post-3')]
