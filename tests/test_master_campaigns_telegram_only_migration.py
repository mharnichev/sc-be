import importlib.util
from pathlib import Path


def load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0064_master_campaigns_telegram_only.py"
    spec = importlib.util.spec_from_file_location("master_campaigns_telegram_only", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_master_campaigns_telegram_only_migration_follows_current_head():
    migration = load_migration()

    assert migration.revision == "0064_master_telegram_only"
    assert migration.down_revision == "0063_master_lifecycle_messages"
