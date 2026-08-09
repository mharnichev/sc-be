from __future__ import annotations

import importlib.util
from pathlib import Path


def load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0059_repair_redirected_master_calendars.py"
    )
    spec = importlib.util.spec_from_file_location("redirect_calendar_repair", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_redirect_calendar_repair_has_conflict_safe_sql_contract(monkeypatch) -> None:
    migration = load_migration()
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    sql = "\n".join(statements)
    assert migration.down_revision == "0058_funnel_no_slot_duration"
    assert "LOCK TABLE" in sql
    assert "IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "WITH RECURSIVE redirect_chain" in sql
    assert "chain.current_master_id <> chain.source_master_id" in sql
    assert "chain is cyclic, broken, or ends at an inactive master" in sql
    assert "redirect chain contains an inactive master" in sql
    assert "has no active target mapping" in sql
    assert "has ambiguous target mappings" in sql
    assert "maps multiple selected services" in sql
    assert "overlaps booking" in sql
    assert "redirected_from_master_id = COALESCE" in sql
    assert "UPDATE waitlist_offers" in sql
    assert "source_master_id = COALESCE" in sql
    assert "waitlist hold %s overlaps confirmed booking" in sql
    assert "WITH affected_holds AS" in sql
    assert sql.count("offer.expires_at > now()") >= 2
    assert "waitlist hold %s overlaps waitlist hold" in sql
    assert "UPDATE master_time_blocks" in sql
    assert "row_number() OVER" in sql
    assert "previous_max_end < start_at" in sql
    assert "INSERT INTO master_availability_windows" in sql


def test_redirect_calendar_repair_downgrade_does_not_guess_old_ownership(monkeypatch) -> None:
    migration = load_migration()

    def unexpected_execute(_statement: str) -> None:
        raise AssertionError("irreversible repair must not guess prior source ownership")

    monkeypatch.setattr(migration.op, "execute", unexpected_execute)
    migration.downgrade()
