import importlib.util
from pathlib import Path


def load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0097_enrich_master_booking_notification.py"
    )
    spec = importlib.util.spec_from_file_location("enrich_master_booking_notification", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enriched_master_booking_notification_migration_updates_current_scenario():
    migration = load_migration()

    assert migration.revision == "0097_master_booking_notification"
    assert migration.down_revision == "0096_inventory_workflow"
    assert "🆔" not in migration.CREATED_BODY
    assert "{comment_line}" in migration.CREATED_BODY
    assert "{promotion_line}" in migration.CREATED_BODY
    assert "{price_line}" in migration.CREATED_BODY
    assert "{booking_url}" in migration.CREATED_BODY
