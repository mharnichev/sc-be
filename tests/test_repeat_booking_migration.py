import importlib.util
from pathlib import Path


def load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0061_repeat_booking_offers.py"
    spec = importlib.util.spec_from_file_location("repeat_booking_offers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repeat_booking_migration_follows_current_head_and_uses_dedicated_enum():
    migration = load_migration()
    assert migration.revision == "0061_repeat_booking_offers"
    assert migration.down_revision == "0060_booking_sms_links"
    assert migration.offer_status.name == "repeatbookingofferstatus"
