import importlib.util
from pathlib import Path


def load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0066_booking_sources.py"
    spec = importlib.util.spec_from_file_location("booking_sources", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_booking_source_migration_follows_current_head_and_backfills_known_channels():
    migration = load_migration()
    source = Path(migration.__file__).read_text()

    assert migration.revision == "0066_booking_sources"
    assert migration.down_revision == "0065_brand_visibility"
    assert migration.booking_source.name == "bookingsource"
    assert "booking_funnel_events" in source
    assert "telegram_bot_sessions" in source
    assert "partial historical backfill" in source
