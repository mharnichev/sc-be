"""Rehearse only the new nullable content columns in an isolated database."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_migration_keeps_existing_content_and_hierarchy(monkeypatch):
    url = os.getenv('PRODUCT_NAMES_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set PRODUCT_NAMES_TEST_DATABASE_URL to isolated PostgreSQL')
    parsed = make_url(url)
    assert parsed.host in {'localhost', '127.0.0.1'} and parsed.database == 'product_names_test'
    engine = create_async_engine(url)
    schema = 'content_migration_' + uuid4().hex
    path = Path(__file__).parents[1] / 'alembic/versions/0138_shop_content_en.py'
    spec = importlib.util.spec_from_file_location('content_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection, direction):
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
        getattr(migration, direction)()

    try:
        async with engine.begin() as con:
            await con.execute(text(f'CREATE SCHEMA "{schema}"'))
            await con.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await con.execute(text('CREATE TABLE products (id integer PRIMARY KEY, description text, short_description text, ingredients text, stock_quantity integer)'))
            await con.execute(text('CREATE TABLE categories (id integer PRIMARY KEY, description text, parent_id integer)'))
            await con.execute(text("INSERT INTO products VALUES (1,'<p>Шампунь</p>','Догляд','Aqua',7)"))
            await con.execute(text("INSERT INTO categories VALUES (1,'Для волосся',NULL),(2,'Шампуні',1)"))
            for direction in ['upgrade', 'downgrade', 'upgrade']:
                await con.run_sync(run, direction)
                assert (await con.execute(text('SELECT * FROM products' if direction == 'downgrade' else 'SELECT id,description,short_description,ingredients,stock_quantity FROM products'))).one() == (1, '<p>Шампунь</p>', 'Догляд', 'Aqua', 7)
                assert (await con.execute(text('SELECT id,description,parent_id FROM categories ORDER BY id'))).all() == [(1,'Для волосся',None),(2,'Шампуні',1)]
            assert await con.scalar(text('SELECT description_en FROM products WHERE id=1')) is None
            assert await con.scalar(text('SELECT description_en FROM categories WHERE id=1')) is None
    finally:
        async with engine.begin() as con:
            await con.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
