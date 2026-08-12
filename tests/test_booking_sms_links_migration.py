import importlib.util
from pathlib import Path


def load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0060_manage_booking_sms_links.py"
    )
    spec = importlib.util.spec_from_file_location("booking_sms_links", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_booking_sms_link_migration_appends_only_missing_variables() -> None:
    migration = load_migration()
    legacy = "Повідомлення про запис."
    migrated = migration.ensure_activity_links(legacy)

    assert migrated == (
        "Повідомлення про запис.\n"
        "Переглянути: {manage_url}\n"
        "Скасувати: {cancel_url}"
    )
    assert migration.ensure_activity_links(migrated) == migrated


def test_booking_sms_link_migration_preserves_operator_copy_and_existing_syntax() -> None:
    migration = load_migration()
    custom = "Керувати: {{ manage_url }}. Відміна: #cancel_url"

    assert migration.ensure_activity_links(custom) == custom
