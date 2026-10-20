"""Additional release gates; only private PostgreSQL and ASGI fixtures."""
import asyncio
import importlib.util
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.category import Category
from app.models.product import Product
from app.utils import backfill_category_names_en, backfill_product_names_en
from tests.test_catalog_assembly_http_integration import catalog, create_app
from tests.test_segments_integration import database as catalog_database
from tests.test_shop_fiscal_integration import database, template_database
from tests.test_inventory_integration import checkout_dependencies
from tests.test_master_mono_integration import mock_provider, prepared


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(params=["products", "categories"])
async def backfill_fixture(request, monkeypatch, tmp_path):
    url = os.environ["PRODUCT_NAMES_TEST_DATABASE_URL"]
    engine = create_async_engine(url)
    schema = "english_release_" + uuid4().hex
    async with engine.begin() as con:
        assert await con.scalar(text("SELECT datcollate FROM pg_database WHERE datname=current_database()")) == "en_US.utf8"
        await con.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    table = request.param
    category = table == "categories"
    tool = backfill_category_names_en if category else backfill_product_names_en
    names = [{"id": i, "slug": f"item-{i}", "name_original": f"Original {i}", "name_en": f"English {i}"}
             for i in (1, 2)]
    for item in names:
        item.update({"parent_id": None if item["id"] == 1 else 1} if category else {"product_type_en": "Care"})
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"count": 2, table: names}))
    monkeypatch.setattr(tool, "AsyncSessionLocal", async_sessionmaker(scoped, expire_on_commit=False))
    try:
        async with scoped.begin() as con:
            extra = "parent_id integer" if category else "stock_quantity integer NOT NULL DEFAULT 7, product_type text"
            await con.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY,name text,slug text,{extra})"))
            for item in names:
                fields = "id,name,slug,parent_id" if category else "id,name,slug"
                values = ":id,:name_original,:slug,:parent_id" if category else ":id,:name_original,:slug"
                await con.execute(text(f"INSERT INTO {table} ({fields}) VALUES ({values})"), item)
            number = "0136_category_names_en" if category else "0135_product_names_en"
            spec = importlib.util.spec_from_file_location(number, Path(__file__).parents[1] / f"alembic/versions/{number}.py")
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            def upgrade(connection):
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
            await con.run_sync(upgrade)
        yield tool, table, scoped, input_path, tmp_path
    finally:
        await scoped.dispose()
        async with engine.begin() as con:
            await con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def rows(engine, table):
    async with engine.connect() as con:
        return (await con.execute(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id"))).scalars().all()


@pytest.mark.anyio
async def test_report_exclusive_create_race_never_writes_database(backfill_fixture, monkeypatch):
    tool, table, engine, source, directory = backfill_fixture
    report = directory / "existing.json"
    report.write_bytes(b"immutable previous report\n")
    before = await rows(engine, table)
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: False if p == report else real_exists(p))
    with pytest.raises(FileExistsError):
        await tool.backfill(input_path=source, report_path=report, apply=True)
    assert report.read_bytes() == b"immutable previous report\n"
    assert await rows(engine, table) == before


@pytest.mark.anyio
async def test_postgres_source_translation_parent_and_id_cas(backfill_fixture):
    tool, table, engine, source, directory = backfill_fixture
    changes = [("name", "Changed"), ("slug", "changed"), ("name_en", "Manual")]
    changes += [("parent_id", 42)] if table == "categories" else [("product_type_en", "Manual")]
    for index, (field, value) in enumerate(changes):
        async with engine.begin() as con:
            previous = await con.scalar(text(f"SELECT {field} FROM {table} WHERE id=2"))
            await con.execute(text(f"UPDATE {table} SET {field}=:v WHERE id=2"), {"v": value})
        before = await rows(engine, table)
        for apply in (False, True):
            report = directory / f"rejected-{index}-{apply}.json"
            with pytest.raises(ValueError, match="Source changed|Existing .* differs"):
                await tool.backfill(input_path=source, report_path=report, apply=apply)
            assert not report.exists()
            assert await rows(engine, table) == before
        async with engine.begin() as con:
            await con.execute(text(f"UPDATE {table} SET {field}=:v WHERE id=2"), {"v": previous})
    async with engine.begin() as con:
        await con.execute(text(f"INSERT INTO {table} (id,name,slug) VALUES (3,'New','new')"))
    before = await rows(engine, table)
    with pytest.raises(ValueError, match="Catalog IDs changed"):
        await tool.backfill(input_path=source, report_path=directory / "ids.json", apply=True)
    assert await rows(engine, table) == before


@pytest.mark.anyio
async def test_second_row_failure_report_is_not_commit_proof(backfill_fixture):
    tool, table, engine, source, directory = backfill_fixture
    before = await rows(engine, table)
    async with engine.begin() as con:
        await con.execute(text(f"ALTER TABLE {table} ADD CHECK (id<>2 OR name_en IS NULL)"))
    report = directory / "failed.json"
    with pytest.raises(DBAPIError, match="CheckViolationError"):
        await tool.backfill(input_path=source, report_path=report, apply=True)
    assert await rows(engine, table) == before
    record = json.loads(report.read_text())
    assert record["status"] == "prepared_apply" and len(record["updates"]) == 2
    async with engine.begin() as con:
        await con.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {table}_check"))
    assert (await tool.backfill(input_path=source, report_path=directory / "new-apply.json", apply=True))["status"] == "committed"
    assert json.loads(report.read_text()) == record
    assert (await tool.backfill(input_path=source, report_path=directory / "retry.json", apply=True))["updated"] == 0


@pytest.mark.anyio
async def test_actual_english_http_search_breadcrumbs_and_nulls(catalog_database):
    # Reuse the actual populated PostgreSQL catalog fixture with a local alias.
    fixture = catalog.__wrapped__(catalog_database)
    await fixture
    async with catalog_database() as session:
        (await session.get(Product, 101)).name_en = "English shaving cream"
        (await session.get(Product, 101)).product_type_en = "English foam"
        (await session.get(Category, 11)).name_en = "English catalog"
        (await session.get(Category, 12)).name_en = "English care"
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=create_app(catalog_database)), base_url="http://english.test") as client:
        for search in ("shaving", "foam"):
            result = await client.get("/products", params={"search": search})
            assert result.status_code == 200 and [p["id"] for p in result.json()["items"]] == [101]
        result = await client.get("/products/101")
        assert result.status_code == 200
        body = result.json()
        assert body["name"] == "Alpha" and body["name_en"] == "English shaving cream"
        assert [(c["name"], c["name_en"]) for c in body["category_tree"]] == [
            ("Catalog", "English catalog"), ("Care", "English care")]
        untranslated = await client.get("/products/102")
        assert untranslated.status_code == 200 and untranslated.json()["name"] == "Beta"
        assert untranslated.json()["name_en"] is None
        categories = await client.get("/categories", params={"search": "English care"})
        assert categories.status_code == 200 and [c["id"] for c in categories.json()["items"]] == [12]


@pytest.mark.anyio
async def test_134_136_134_136_preserves_original_catalog(template_database):
    from app.core.config import settings
    engine = create_async_engine(settings.database_url)
    config = template_database[3]
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Category(id=9901, name="Original root", slug="english-release-root", name_en="English root"))
            await session.flush()
            session.add(Category(id=9902, name="Original child", slug="english-release-child", parent_id=9901))
            await session.flush()
            session.add(Product(id=9901, name="Original product", slug="english-release-product", price=Decimal("100.00"),
                                category_id=9902, stock_quantity=7, reserved_quantity=0,
                                name_en="English product", product_type_en="Care"))
            await session.commit()
        async def originals():
            async with engine.connect() as con:
                return {table: (await con.execute(text(
                    f"SELECT (to_jsonb(t)-ARRAY['name_en','product_type_en'])::text FROM {table} t ORDER BY id"
                ))).scalars().all() for table in ("products", "categories")}
        before = await originals()
        assert all(before.values())
        async with engine.begin() as con:
            assert await con.scalar(text("SELECT version_num FROM alembic_version")) == "0136_category_names_en"
        await asyncio.to_thread(command.downgrade, config, "0134_shop_canonical_collation")
        await asyncio.to_thread(command.upgrade, config, "0136_category_names_en")
        await asyncio.to_thread(command.downgrade, config, "0134_shop_canonical_collation")
        await asyncio.to_thread(command.upgrade, config, "0136_category_names_en")
        async with engine.connect() as con:
            assert await con.scalar(text("SELECT version_num FROM alembic_version")) == "0136_category_names_en"
            assert await con.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND ((table_name='products' AND column_name IN ('name_en','product_type_en')) OR (table_name='categories' AND column_name='name_en'))")) == 3
            assert await con.scalar(text("SELECT count(*) FROM products WHERE name_en IS NOT NULL OR product_type_en IS NOT NULL")) == 0
            assert await con.scalar(text("SELECT count(*) FROM categories WHERE name_en IS NOT NULL")) == 0
        assert await originals() == before
    finally:
        async with engine.begin() as con:
            await con.execute(text("DELETE FROM products WHERE id=9901"))
            await con.execute(text("DELETE FROM categories WHERE id=9902"))
            await con.execute(text("DELETE FROM categories WHERE id=9901"))
        await engine.dispose()


@pytest.mark.anyio
async def test_master_history_refusal_at_136_and_english_does_not_rewrite_order(database, template_database, checkout_dependencies, mock_provider):
    _, order_id, _ = await prepared(database, mock_provider)
    async with database() as session:
        before = (await session.execute(text("SELECT row_to_json(t)::text FROM order_items t WHERE order_id=:id ORDER BY id"), {"id": order_id})).scalars().all()
        assert before
        await session.execute(text("UPDATE products SET name_en='English only',product_type_en='Care'"))
        await session.commit()
        after = (await session.execute(text("SELECT row_to_json(t)::text FROM order_items t WHERE order_id=:id ORDER BY id"), {"id": order_id})).scalars().all()
        assert after == before
    with pytest.raises(DBAPIError, match="populated ledger cannot downgrade"):
        await asyncio.to_thread(command.downgrade, template_database[3], "0132_mono_checkbox_poll")
    async with database() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0136_category_names_en"
        assert await session.scalar(text("SELECT count(*) FROM master_mono_create_requests WHERE order_id=:id"), {"id": order_id}) == 1
