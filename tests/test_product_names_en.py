from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.product import ProductCreate, ProductUpdate, PublicProductResponse, ProductResponse
from app.services.catalog_assembly import volume_variant_responses


def test_english_names_are_optional_and_updates_preserve_source_fields():
    created = ProductCreate(name="Крем", slug="cream", price=100, name_en="Shaving cream", product_type_en="Cream")
    assert created.name == "Крем"
    assert created.name_en == "Shaving cream"
    assert ProductCreate(name="Крем", slug="cream", price=100).name_en is None
    assert ProductUpdate(name_en=None).model_dump(exclude_unset=True) == {"name_en": None}
    assert ProductUpdate(name_en="Cream").model_dump(exclude_unset=True) == {"name_en": "Cream"}
    assert ProductUpdate().model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize("schema", [ProductCreate, ProductUpdate])
@pytest.mark.parametrize("field,value", [("name_en", "x"), ("name_en", "x" * 256), ("product_type_en", "x" * 256)])
def test_english_name_write_limits(schema, field, value):
    with pytest.raises(ValidationError):
        schema(**dict(name="Крем", slug="cream", price=100, **{field: value}))


@pytest.mark.parametrize("schema", [PublicProductResponse, ProductResponse])
def test_product_responses_keep_both_names(schema):
    values = {name: None for name, field in schema.model_fields.items() if field.is_required()}
    values.update(id=1, name="Крем", name_en="Cream", product_type="Крем", product_type_en="Cream",
                  slug="cream", price=Decimal("100"), is_active=True, stock_quantity=1,
                  reserved_quantity=0, available_quantity=1, allow_backorder=False,
                  created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    result = schema.model_validate(values).model_dump()
    assert result["name"] == "Крем"
    assert result["name_en"] == "Cream"
    assert result["product_type_en"] == "Cream"


def test_volume_variants_expose_english_name_without_replacing_source():
    product = SimpleNamespace(id=1, name="Крем 100 мл", model_name="Original model", name_en="Cream 100 ml",
                              slug="cream", sku=None, volume_ml=100, images=[], image_variants=[],
                              attributes_json=None, image_url=None, availability_status=None)
    price = SimpleNamespace(base_price=Decimal("100"), price=Decimal("100"))
    visibility = SimpleNamespace(is_available_for_purchase=lambda _: True)
    variant = volume_variant_responses([product], {1: price}, visibility)[0]
    assert variant.name == "Original model"
    assert variant.name_en == "Cream 100 ml"


@pytest.mark.parametrize("search", ["shaving", "Foam", "nourishing", "lightweight"])
def test_catalog_search_matches_english_fields_with_real_sql(monkeypatch, search):
    import sqlite3
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.dialects import sqlite
    from app.api.v1.routes import products as routes
    from app.core.database import get_db_session
    from app.models.product import Product
    from app.services.catalog_visibility import CatalogVisibility

    database = sqlite3.connect(":memory:", check_same_thread=False)
    database.execute("CREATE TABLE products (id INTEGER, name TEXT, name_en TEXT, old_name TEXT, model_name TEXT, product_type TEXT, product_type_en TEXT, description TEXT, description_en TEXT, short_description TEXT, short_description_en TEXT, sku TEXT, is_active BOOLEAN, category_id INTEGER, stock_quantity INTEGER, reserved_quantity INTEGER, created_at TEXT)")
    database.executemany("INSERT INTO products VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, ?, NULL, 1, 0, '2026-01-01')", [
        (1, "Крем", "Shaving cream", "Foam", True),
        (2, "Інший", None, None, True),
        (3, "Прихований", "Shaving cream", "Foam", False),
    ])
    database.execute("UPDATE products SET description_en = ?, short_description_en = ? WHERE id IN (1, 3)", ("Nourishing care", "Lightweight formula"))
    selected = []

    async def load(_):
        return CatalogVisibility.from_categories([])

    async def paginate(_, stmt, *, limit, offset):
        query = stmt.with_only_columns(Product.id).options()
        sql = str(query.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        ids = [row[0] for row in database.execute(sql)]
        selected.extend(ids)
        return [], len(ids)

    async def empty(*args, **kwargs):
        return {}

    monkeypatch.setattr(routes.CatalogVisibility, "load", load)
    monkeypatch.setattr(routes, "_paginate", paginate)
    monkeypatch.setattr(routes, "_review_stats", empty)
    monkeypatch.setattr(routes.shop_promotion_service, "price_products", empty)
    app = FastAPI()
    app.include_router(routes.public_router, prefix="/products")
    app.dependency_overrides[get_db_session] = lambda: object()
    try:
        with TestClient(app) as client:
            response = client.get("/products", params={"search": search})
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert selected == [1]
    finally:
        database.close()


def test_migration_adds_nullable_columns_and_reverses_them(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "alembic/versions/0135_product_names_en.py"
    spec = importlib.util.spec_from_file_location("product_names_en_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    added, removed = [], []
    monkeypatch.setattr(migration.op, "add_column", lambda table, column: added.append((table, column)))
    monkeypatch.setattr(migration.op, "drop_column", lambda table, column: removed.append((table, column)))
    migration.upgrade()
    assert migration.down_revision == "0134_shop_canonical_collation"
    assert [(table, column.name, column.type.length, column.nullable) for table, column in added] == [
        ("products", "name_en", 255, True), ("products", "product_type_en", 255, True)]
    migration.downgrade()
    assert removed == [("products", "product_type_en"), ("products", "name_en")]
