import importlib.util
from pathlib import Path


def load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0063_master_booking_lifecycle_messages.py"
    spec = importlib.util.spec_from_file_location("master_booking_lifecycle_messages", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_master_booking_lifecycle_migration_seeds_both_master_scenarios():
    migration = load_migration()

    assert migration.revision == "0063_master_lifecycle_messages"
    assert migration.down_revision == "0062_master_schedule_reminders"
    assert migration.message_channel.name == "messagechannel"
    assert migration.message_delivery_status.name == "messagedeliverystatus"
    assert migration.CREATED_NAME == "Сповіщення в момент запису"
    assert migration.CANCELLED_NAME == "Сповіщення про скасування запису"
    assert "{customer_name}" in migration.CREATED_BODY
    assert "{customer_name}" in migration.CANCELLED_BODY
