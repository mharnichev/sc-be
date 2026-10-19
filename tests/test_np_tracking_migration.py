"""Full Alembic/backfill regression in a disposable opt-in local test database."""
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def test_full_upgrade_and_legacy_backfill(monkeypatch):
    raw = os.environ.get('INVENTORY_TEST_DATABASE_URL')
    if not raw:
        pytest.skip('Set INVENTORY_TEST_DATABASE_URL to an isolated local PostgreSQL database')
    base = make_url(raw).set(drivername='postgresql+psycopg2')
    assert base.host in {'127.0.0.1', 'localhost', '::1'} and 'test' in base.database.lower()
    name = 'tracking_migration_test_' + uuid4().hex
    admin = create_engine(base, isolation_level='AUTOCOMMIT')
    # A schema-only search_path lets SQLAlchemy enum existence checks see enums
    # from public while CREATE TABLE cannot resolve them in the selected schema.
    # Isolate the whole catalog, including types, from the shared test database.
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    isolated = base.set(database=name)
    engine = create_engine(isolated)
    try:
        monkeypatch.delenv('PGOPTIONS', raising=False)
        monkeypatch.setenv('DATABASE_URL', isolated.set(drivername='postgresql+asyncpg').render_as_string(hide_password=False))
        from app.core.config import settings
        monkeypatch.setattr(settings, 'database_url', os.environ['DATABASE_URL'])
        config = Config('alembic.ini')
        command.upgrade(config, '0114_mock_checkbox')
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO orders (customer_name,customer_phone,status,total_amount,subtotal_amount,discount_amount,shipping_company,tracking_number,simulation,revision) VALUES ('Fixture','fixture','completed',100,100,0,'nova_poshta','20450000000001',false,1)"))
        command.upgrade(config, 'head')
        with engine.begin() as connection:
            row = connection.execute(text('SELECT simulation, status, tracking_status, last_checked_at FROM order_shipments')).one()
            assert row == (False, 'requires_review', None, None), row
            assert connection.scalar(text('SELECT status::text FROM orders')) == 'completed'
            assert connection.scalar(text('SELECT count(*) FROM shipment_tracking_poll_state')) == 1
            connection.execute(text("UPDATE order_shipments SET tracking_status='delivered', raw_status='9',last_checked_at=now()"))
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
