"""Durable reports and operator recovery boundaries on synthetic local PG only."""
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.utils import backfill_category_names_en as categories
from app.utils import backfill_product_names_en as products
from app.utils import backfill_english_report as reports
from tests.test_english_release_gates import backfill_fixture, rows


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_missing_report_parent_fails_without_creation_or_database_mutation(backfill_fixture):
    tool, table, engine, source, directory = backfill_fixture
    before = await rows(engine, table)
    parent = directory / "not-precreated" / "private-reports"
    with pytest.raises(FileNotFoundError):
        await tool.backfill(input_path=source, report_path=parent / "report.json", apply=True)
    assert not (directory / "not-precreated").exists()
    assert await rows(engine, table) == before


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["write", "file_fsync", "directory_fsync"])
async def test_report_persistence_failure_prevents_every_update(backfill_fixture, monkeypatch, failure):
    tool, table, engine, source, directory = backfill_fixture
    before = await rows(engine, table)
    updates = []
    def sql(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", sql)
    real_fsync = os.fsync
    def fsync(descriptor):
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if failure == ("directory_fsync" if is_directory else "file_fsync"):
            raise OSError("synthetic persistence failure")
        return real_fsync(descriptor)
    monkeypatch.setattr(reports.os, "fsync", fsync)
    if failure == "write":
        def broken_write(value, handle, **kwargs):
            handle.write('{"partial":')
            raise OSError("synthetic persistence failure")
        monkeypatch.setattr(reports.json, "dump", broken_write)
    try:
        with pytest.raises(OSError, match="synthetic persistence failure"):
            await tool.backfill(input_path=source, report_path=directory / "failed-report.json", apply=True)
        assert updates == []
        assert await rows(engine, table) == before
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", sql)


@pytest.mark.anyio
async def test_same_input_bytes_validated_hashed_and_durable_before_update(backfill_fixture, monkeypatch):
    tool, table, engine, source, directory = backfill_fixture
    original = source.read_bytes()
    real_read = Path.read_bytes
    reads = []
    def read(path):
        data = real_read(path)
        if path == source:
            reads.append(data)
            path.write_bytes(b"not the validated input anymore")
        return data
    monkeypatch.setattr(Path, "read_bytes", read)
    order = []
    real_fsync = os.fsync
    def fsync(descriptor):
        order.append("directory_fsync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file_fsync")
        return real_fsync(descriptor)
    monkeypatch.setattr(reports.os, "fsync", fsync)
    def sql(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE"):
            order.append("update")
    event.listen(engine.sync_engine, "before_cursor_execute", sql)
    report_path = directory / "durable.json"
    try:
        result = await tool.backfill(input_path=source, report_path=report_path, apply=True)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", sql)
    assert result["status"] == "committed" and result["updated"] == 2
    assert reads == [original]
    assert order == ["file_fsync", "directory_fsync", "update", "update"]
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    report = json.loads(report_path.read_text())
    assert report["report_version"] == 1 and report["entity"] == table
    assert report["input_sha256"] == hashlib.sha256(original).hexdigest()
    assert report["status"] == "prepared_apply"
    for record in report["records"]:
        assert record["source"]["name"] == f"Original {record['id']}"
        assert record["source"]["slug"] == f"item-{record['id']}"
        assert record["desired"]["name_en"] == f"English {record['id']}"
        assert all(value is None for value in record["previous"].values())
        assert ("parent_id" if table == "categories" else "product_type") in record["source"]


async def operator_restore_exact_previous(engine, report):
    """Executable fixture example of the reviewed operator CAS contract, not CLI."""
    assert report["entity"] == "products"
    async with engine.begin() as con:
        await con.execute(text("LOCK TABLE products IN SHARE ROW EXCLUSIVE MODE"))
        for record in report["records"]:
            expected = {**record["source"], **record["desired"]}
            predicate = " AND ".join(f"{field} IS NOT DISTINCT FROM :expected_{field}" for field in expected)
            assignments = ", ".join(f"{field}=:previous_{field}" for field in record["previous"])
            result = await con.execute(text(f"UPDATE products SET {assignments} WHERE id=:id AND {predicate}"),
                {"id": record["id"], **{f"expected_{k}": v for k, v in expected.items()},
                 **{f"previous_{k}": v for k, v in record["previous"].items()}})
            if result.rowcount != 1:
                raise ValueError("Operator recovery CAS mismatch; transaction refused")


@pytest.mark.anyio
async def test_product_commits_category_fails_and_operator_recovery_is_cas_safe(monkeypatch, tmp_path):
    url = os.environ["PRODUCT_NAMES_TEST_DATABASE_URL"]
    admin = create_async_engine(url)
    schema = "english_sequence_" + uuid4().hex
    async with admin.begin() as con:
        await con.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(products, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(categories, "AsyncSessionLocal", sessions)
    try:
        async with engine.begin() as con:
            await con.execute(text("CREATE TABLE products (id integer PRIMARY KEY,name text,slug text,product_type text,name_en text,product_type_en text,stock_quantity integer)"))
            await con.execute(text("CREATE TABLE categories (id integer PRIMARY KEY,name text,slug text,parent_id integer,name_en text CHECK(id<>2 OR name_en IS NULL))"))
            await con.execute(text("INSERT INTO products VALUES (1,'Original 1','p1','Original type',NULL,NULL,7),(2,'Original 2','p2',NULL,NULL,NULL,8)"))
            await con.execute(text("INSERT INTO categories VALUES (1,'Root','root',NULL,NULL),(2,'Child','child',1,NULL)"))
        original_products = await rows(engine, "products")
        original_categories = await rows(engine, "categories")
        product_input = tmp_path / "products.json"
        product_input.write_text(json.dumps({"count": 2, "products": [
            {"id": i, "name_original": f"Original {i}", "slug": f"p{i}", "name_en": f"English {i}", "product_type_en": "Care"}
            for i in (1, 2)]}))
        category_input = tmp_path / "categories.json"
        category_input.write_text(json.dumps({"count": 2, "categories": [
            {"id": 1, "name_original": "Root", "slug": "root", "parent_id": None, "name_en": "English root"},
            {"id": 2, "name_original": "Child", "slug": "child", "parent_id": 1, "name_en": "English child"}]}))
        product_report = tmp_path / "product-report.json"
        assert (await products.backfill(input_path=product_input, report_path=product_report, apply=True))["status"] == "committed"
        with pytest.raises(DBAPIError, match="CheckViolationError"):
            await categories.backfill(input_path=category_input, report_path=tmp_path / "category-report.json", apply=True)
        assert await rows(engine, "categories") == original_categories
        committed_products = await rows(engine, "products")
        assert committed_products != original_products
        async with engine.connect() as con:
            assert await con.scalar(text("SELECT count(*) FROM products WHERE name_en IS NOT NULL")) == 2
        report_bytes = product_report.read_bytes()
        report = json.loads(report_bytes)
        # A later source or English edit on row2 must roll back row1's recovery.
        for field, value, restore in [("name_en", "Manual English", "English 2"), ("name", "Manual source", "Original 2")]:
            async with engine.begin() as con:
                await con.execute(text(f"UPDATE products SET {field}=:v WHERE id=2"), {"v": value})
            edited = await rows(engine, "products")
            with pytest.raises(ValueError, match="CAS mismatch"):
                await operator_restore_exact_previous(engine, report)
            assert await rows(engine, "products") == edited
            async with engine.begin() as con:
                await con.execute(text(f"UPDATE products SET {field}=:v WHERE id=2"), {"v": restore})
        await operator_restore_exact_previous(engine, report)
        assert await rows(engine, "products") == original_products
        assert await rows(engine, "categories") == original_categories
        assert product_report.read_bytes() == report_bytes
    finally:
        await engine.dispose()
        async with admin.begin() as con:
            await con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
