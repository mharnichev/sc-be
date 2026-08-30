import importlib.util
from pathlib import Path


def load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0062_master_schedule_reminders.py"
    spec = importlib.util.spec_from_file_location("master_schedule_reminders", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_master_schedule_reminder_migration_follows_current_head_and_seeds_messaging_campaign():
    migration = load_migration()

    assert migration.revision == "0062_master_schedule_reminders"
    assert migration.down_revision == "0061_repeat_booking_offers"
    assert migration.message_channel.name == "messagechannel"
    assert "{coverage_percent}" in migration.TEMPLATE_BODY
