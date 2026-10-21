"""Opt-in rehearsal on a dedicated local test database; never uses app DB."""
import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.utils import backfill_product_names_en as tool


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_postgres_migration_dry_run_commit_retry_rollback(monkeypatch, tmp_path):
    url = os.getenv("PRODUCT_NAMES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set PRODUCT_NAMES_TEST_DATABASE_URL to isolated PostgreSQL")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.database == "product_names_test"
    engine = create_async_engine(url)
    schema = "names_" + uuid4().hex
    async with engine.begin() as con:
        await con.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    monkeypatch.setattr(tool, "AsyncSessionLocal", async_sessionmaker(scoped, expire_on_commit=False))
    path = Path(__file__).parents[1] / "alembic/versions/0135_product_names_en.py"
    spec = importlib.util.spec_from_file_location("names_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection, direction):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        getattr(migration, direction)()

    items = [{"id": i, "name_original": f"Товар {i}", "slug": f"product-{i}",
              "name_en": f"Product {i}", "product_type_en": "Hair care"} for i in [1, 2]]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"count": 2, "products": items}))
    try:
        async with scoped.begin() as con:
            await con.execute(text("CREATE TABLE products (id integer PRIMARY KEY, name text, slug text, stock_quantity integer, product_type text)"))
            for item in items:
                await con.execute(text("INSERT INTO products (id,name,slug,stock_quantity) VALUES (:id,:name_original,:slug,7)"), item)
            await con.run_sync(migrate, "upgrade")
        dry = await tool.backfill(input_path=input_path, report_path=tmp_path / "dry.json", apply=False)
        assert dry == {"status": "dry_run", "count": 2, "updated": 2, "unchanged": 0}
        async with scoped.connect() as con:
            assert await con.scalar(text("SELECT count(*) FROM products WHERE name_en IS NOT NULL")) == 0
        applied = await tool.backfill(input_path=input_path, report_path=tmp_path / "apply.json", apply=True)
        assert applied["updated"] == 2 and applied["status"] == "committed"
        record = (tmp_path / "apply.json").read_bytes()
        assert json.loads(record)["status"] == "prepared_apply"
        assert all(x["previous"] == {"name_en": None, "product_type_en": None} for x in json.loads(record)["updates"])
        assert (await tool.backfill(input_path=input_path, report_path=tmp_path / "retry.json", apply=True))["updated"] == 0
        assert (tmp_path / "apply.json").read_bytes() == record
        async with scoped.begin() as con:
            rows = (await con.execute(text("SELECT name,stock_quantity FROM products ORDER BY id"))).all()
            assert rows == [("Товар 1", 7), ("Товар 2", 7)]
            await con.execute(text("UPDATE products SET name_en=NULL, product_type_en=NULL"))
            # The second update fails after the first succeeds: all data must roll back.
            await con.execute(text("ALTER TABLE products ADD CHECK (id <> 2 OR name_en IS NULL)"))
        with pytest.raises(Exception, match="CheckViolationError"):
            await tool.backfill(input_path=input_path, report_path=tmp_path / "failed.json", apply=True)
        async with scoped.begin() as con:
            assert await con.scalar(text("SELECT count(*) FROM products WHERE name_en IS NOT NULL")) == 0
            await con.run_sync(migrate, "downgrade")
            assert await con.scalar(text("SELECT count(*) FROM products")) == 2
            await con.run_sync(migrate, "upgrade")
    finally:
        await scoped.dispose()
        async with engine.begin() as con:
            await con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
