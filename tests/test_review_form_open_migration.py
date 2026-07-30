from __future__ import annotations

import importlib.util

from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _migration_module():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0051_review_form_open_events.py"
    )
    spec = importlib.util.spec_from_file_location(
        "review_form_open_events_migration",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_review_form_open_migration_creates_empty_marker_table_and_unique_request_key() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite://")

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        migration.op = operations
        migration.upgrade()

        inspector = inspect(connection)
        assert {
            "analytics_tracking_markers",
            "review_form_open_events",
        }.issubset(inspector.get_table_names())
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "review_form_open_events"
            )
        } == {"uq_review_form_open_events_review_request_id"}
        marker_count = connection.execute(
            text(
                "SELECT COUNT(*) "
                "FROM analytics_tracking_markers"
            )
        ).scalar_one()
        assert marker_count == 0

        migration.downgrade()
        assert "review_form_open_events" not in inspect(connection).get_table_names()
        assert "analytics_tracking_markers" not in inspect(connection).get_table_names()
