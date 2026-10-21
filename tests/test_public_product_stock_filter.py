"""Exercise public stock filtering through HTTP and real SQLite predicates."""
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import sqlite

from app.api.v1.routes import products as routes
from app.core.database import get_db_session
from app.models.product import Product
from app.services.catalog_visibility import CatalogVisibility


@pytest.fixture
def stock_catalog(monkeypatch):
    database = sqlite3.connect(":memory:", check_same_thread=False)
    database.execute("CREATE TABLE products (id INTEGER, is_active BOOLEAN, category_id INTEGER, stock_quantity INTEGER, reserved_quantity INTEGER, created_at TEXT)")
    # Sold out, fully reserved, available, null reservation, over-reserved,
    # hidden, and hidden category. Supplier status/backorders do not create stock.
    database.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", [
        (1, True, None, 0, 0, '2026-01-01'),
        (2, True, None, 3, 3, '2026-01-01'),
        (3, True, None, 4, 1, '2026-01-01'),
        (4, True, None, 2, None, '2026-01-01'),
        (5, True, None, 1, 2, '2026-01-01'),
        (6, False, None, 5, 0, '2026-01-01'),
        (7, True, 99, 5, 0, '2026-01-01'),
    ])
    selected_pages = []

    async def visibility(_session):
        return CatalogVisibility.from_categories([])

    async def paginate(_session, stmt, *, limit, offset):
        query = stmt.with_only_columns(Product.id).options()
        sql = str(query.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        ids = [row[0] for row in database.execute(sql).fetchall()]
        selected_pages.append(ids[offset:offset + limit])
        return [], len(ids)

    async def prices(*args, **kwargs):
        return {}

    async def stats(*args, **kwargs):
        return {}

    monkeypatch.setattr(routes.CatalogVisibility, 'load', visibility)
    monkeypatch.setattr(routes, '_paginate', paginate)
    monkeypatch.setattr(routes.shop_promotion_service, 'price_products', prices)
    monkeypatch.setattr(routes, '_review_stats', stats)
    app = FastAPI()
    app.include_router(routes.public_router, prefix='/public/products')
    app.dependency_overrides[get_db_session] = lambda: object()
    with TestClient(app) as client:
        yield client, selected_pages
    database.close()


def test_stock_filter_applies_before_total_and_pagination(stock_catalog):
    client, pages = stock_catalog
    first = client.get('/public/products', params={'in_stock': 'true', 'limit': 1, 'offset': 0})
    second = client.get('/public/products', params={'in_stock': 'true', 'limit': 1, 'offset': 1})
    assert first.status_code == second.status_code == 200
    assert first.json()['total'] == second.json()['total'] == 2
    assert pages == [[4], [3]]


@pytest.mark.parametrize(('query', 'expected'), [('', [4, 3, 5, 2, 1]), ('?in_stock=false', [5, 2, 1])])
def test_optional_stock_filter_preserves_catalog_default(stock_catalog, query, expected):
    client, pages = stock_catalog
    response = client.get('/public/products' + query)
    assert response.status_code == 200
    assert response.json()['total'] == len(expected)
    assert pages == [expected]


def test_invalid_stock_filter_is_rejected(stock_catalog):
    client, pages = stock_catalog
    assert client.get('/public/products?in_stock=invalid').status_code == 422
    assert pages == []
