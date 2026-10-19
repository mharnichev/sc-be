"""Execute 0110 DDL against a disposable schema, preserving existing rows."""
import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from app.core.database import Base
from app.models.order import Order
from app.models.shop import DeliveryCache


def test_directory_remains_in_single_release_migration_chain():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_heads() == ["0129_shop_ingress_limits"]
    assert scripts.get_revision("0129_shop_ingress_limits").down_revision == "0128_shop_payment_resolutions"
    assert "0110_nova_poshta_directory" in {revision.revision for revision in scripts.walk_revisions()}
    assert scripts.get_revision("0110_nova_poshta_directory").down_revision == "0109_telegram_booking_funnel"


def test_actual_upgrade_downgrade_preserves_existing_rows():
    raw = os.environ.get("INVENTORY_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Set INVENTORY_TEST_DATABASE_URL for isolated local PostgreSQL")
    url = make_url(raw)
    assert url.host in {"localhost", "127.0.0.1", "::1"}
    assert url.database in {"barbershop_platform_test", "sc_be_pilot_test_20261019"}
    assert url.get_backend_name() == "postgresql"
    engine = create_engine(url.set(drivername="postgresql+psycopg2"))
    schema = "np_migration_test_" + uuid4().hex
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0110_nova_poshta_directory.py"
    spec = importlib.util.spec_from_file_location("directory_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_database()")) == url.database
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            Base.metadata.create_all(connection, tables=[
                table for table in Base.metadata.sorted_tables
                if table.name not in {"nova_poshta_settlements", "nova_poshta_branches"}
            ])
            connection.execute(Order.__table__.insert().values(
                id=1, customer_name="Preserve", customer_phone="+380501112233",
                total_amount=100, shipping_payload_json={"legacy": "preserve"},
            ))
            connection.execute(DeliveryCache.__table__.insert().values(
                id=1, cache_key="legacy", payload_json={"cached": "preserve"},
                expires_at=datetime.now(UTC),
            ))
            before = set(inspect(connection).get_table_names())
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                connection.execute(text("""INSERT INTO nova_poshta_settlements
                    VALUES ('settlement', 'city', 'Kyiv', 'kyiv', 0, true, '{}')"""))
                connection.execute(text("""INSERT INTO nova_poshta_branches
                    VALUES ('branch', 'settlement', '2', 2, 'branch', '{}')"""))
                assert connection.scalar(text("SELECT count(*) FROM nova_poshta_branches")) == 1
                assert inspect(connection).get_indexes("nova_poshta_branches")[0]["column_names"] == ["settlement_ref"]
                migration.downgrade()
                assert set(inspect(connection).get_table_names()) == before
                assert connection.scalar(text("SELECT shipping_payload_json FROM orders WHERE id=1")) == {"legacy": "preserve"}
                assert connection.scalar(text("SELECT payload_json FROM delivery_cache WHERE id=1")) == {"cached": "preserve"}
                migration.upgrade()
                assert connection.scalar(text("SELECT count(*) FROM nova_poshta_settlements")) == 0
                migration.downgrade()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
