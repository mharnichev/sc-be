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

from app.utils import backfill_category_names_en as tool


def names():
    return {1: {"id": 1, "slug": "body", "parent_id": None, "name_original": "Для тіла", "name_en": "For Body"}}


def row(**kwargs):
    return {"id": 1, "slug": "body", "parent_id": None, "name": "Для тіла", "name_en": None, **kwargs}


def test_category_plan_preserves_manual_values_and_hierarchy():
    assert tool.plan_updates([row()], names()) == [{"id": 1, "previous": {"name_en": None}, "values": {"name_en": "For Body"}}]
    assert tool.plan_updates([row(name_en="For Body")], names()) == []
    for rows in [[], [row(id=2)], [row(name="Змінено")], [row(slug="changed")], [row(parent_id=7)], [row(name_en="Manual")]]:
        with pytest.raises(ValueError):
            tool.plan_updates(rows, names())


@pytest.mark.parametrize("field,value", [("id", True), ("name_en", "x"), ("name_en", "a" * 256), ("name_en", " For Body"), ("parent_id", True)])
def test_invalid_input_rejected_before_connection(tmp_path, field, value):
    item = names()[1]
    item[field] = value
    path = tmp_path / "names.json"
    path.write_text(json.dumps({"count": 1, "categories": [item]}))
    with pytest.raises(ValueError):
        tool.load_names(path)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_postgres_category_migration_and_guarded_backfill(monkeypatch, tmp_path):
    url = os.getenv("PRODUCT_NAMES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set PRODUCT_NAMES_TEST_DATABASE_URL to isolated PostgreSQL")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost"} and parsed.database == "product_names_test"
    schema = "category_names_" + uuid4().hex
    engine = create_async_engine(url)
    async with engine.begin() as con:
        await con.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    monkeypatch.setattr(tool, "AsyncSessionLocal", async_sessionmaker(scoped, expire_on_commit=False))
    path = Path(__file__).parents[1] / "alembic/versions/0136_category_names_en.py"
    spec = importlib.util.spec_from_file_location("category_names_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection, direction):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        getattr(migration, direction)()

    items = [names()[1], {"id": 2, "slug": "hygiene", "parent_id": 1, "name_original": "Гігієна", "name_en": "Personal Hygiene"}]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"count": 2, "categories": items}))
    try:
        async with scoped.begin() as con:
            await con.execute(text("CREATE TABLE categories (id integer PRIMARY KEY, name text, slug text, parent_id integer)"))
            for item in items:
                await con.execute(text("INSERT INTO categories VALUES (:id,:name_original,:slug,:parent_id)"), item)
            await con.run_sync(migrate, "upgrade")
        dry = await tool.backfill(input_path=input_path, report_path=tmp_path / "dry.json", apply=False)
        assert dry["updated"] == 2 and dry["status"] == "dry_run"
        async with scoped.connect() as con:
            assert await con.scalar(text("SELECT count(*) FROM categories WHERE name_en IS NOT NULL")) == 0
        applied = await tool.backfill(input_path=input_path, report_path=tmp_path / "apply.json", apply=True)
        assert applied["updated"] == 2 and applied["status"] == "committed"
        backup = (tmp_path / "apply.json").read_bytes()
        assert json.loads(backup)["status"] == "prepared_apply"
        assert (await tool.backfill(input_path=input_path, report_path=tmp_path / "retry.json", apply=True))["updated"] == 0
        assert (tmp_path / "apply.json").read_bytes() == backup
        async with scoped.begin() as con:
            assert (await con.execute(text("SELECT name,slug,parent_id FROM categories ORDER BY id"))).all() == [("Для тіла", "body", None), ("Гігієна", "hygiene", 1)]
            await con.execute(text("UPDATE categories SET name_en=NULL"))
            await con.execute(text("ALTER TABLE categories ADD CHECK (id <> 2 OR name_en IS NULL)"))
        with pytest.raises(Exception, match="CheckViolationError"):
            await tool.backfill(input_path=input_path, report_path=tmp_path / "failed.json", apply=True)
        async with scoped.begin() as con:
            assert await con.scalar(text("SELECT count(*) FROM categories WHERE name_en IS NOT NULL")) == 0
            await con.run_sync(migrate, "downgrade")
            assert await con.scalar(text("SELECT count(*) FROM categories")) == 2
            await con.run_sync(migrate, "upgrade")
    finally:
        await scoped.dispose()
        async with engine.begin() as con:
            await con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
