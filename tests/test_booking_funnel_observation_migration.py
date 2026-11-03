"""Real PostgreSQL round trip without touching predecessor history/ledger tables."""

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from tests.test_inventory_integration import anyio_backend, database

pytestmark=pytest.mark.anyio


async def test_additive_migration_round_trip_preserves_existing_tables(database):
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path
    spec=spec_from_file_location('observation_migration',Path(__file__).parents[1]/'alembic/versions/0145_booking_funnel_observations.py')
    migration=module_from_spec(spec);spec.loader.exec_module(migration)
    engine=database.kw['bind']
    async with engine.begin() as connection:
        # Metadata fixtures begin at head; remove just our new table to represent0144.
        await connection.execute(text('DROP TABLE booking_funnel_observation_receipts'))
        await connection.execute(text('DROP TABLE booking_funnel_observations'))
        await connection.execute(text("INSERT INTO booking_funnel_events (event_id_hash,event_type,source,occurred_at) VALUES ('migration-synthetic-marker','booking_start','client',now())"))
        def roundtrip(sync):
            op=Operations(MigrationContext.configure(sync))
            migration.op=op
            before=set(inspect(sync).get_table_names())
            migration.upgrade()
            assert set(inspect(sync).get_table_names())==before|{'booking_funnel_observations','booking_funnel_observation_receipts'}
            assert {row['name'] for row in inspect(sync).get_indexes('booking_funnel_observations') if not row.get('duplicates_constraint')} == {'ix_funnel_observation_period_master','ix_funnel_observation_attempt_request'}
            assert len(inspect(sync).get_unique_constraints('booking_funnel_observations')) == 2
            migration.downgrade()
            assert set(inspect(sync).get_table_names())==before
            assert sync.execute(text("SELECT count(*) FROM booking_funnel_events WHERE event_id_hash='migration-synthetic-marker'")).scalar_one()==1
            migration.upgrade()
        await connection.run_sync(roundtrip)
