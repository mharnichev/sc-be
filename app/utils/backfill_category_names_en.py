"""Apply reviewed English category names atomically; default is a dry run.

The input must cover the complete current category tree and carry id, slug and
name_original for optimistic source checks. Existing different translations
are never overwritten. The report records previous values before commit.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.utils.backfill_english_report import persist_report


def load_names(path: Path) -> dict[int, dict]:
    return parse_names(json.loads(path.read_bytes()))


def parse_names(payload: dict) -> dict[int, dict]:
    items = payload["categories"]
    names: dict[int, dict] = {}
    for item in items:
        category_id = item["id"]
        if type(category_id) is not int or category_id in names:
            raise ValueError("Category IDs must be unique integers")
        for key in ("slug", "name_original", "name_en"):
            value = item[key]
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"Invalid {key} for category {category_id}")
        if not 2 <= len(item["name_en"]) <= 255:
            raise ValueError(f"English value too long or short for category {category_id}")
        if item["parent_id"] is not None and type(item["parent_id"]) is not int:
            raise ValueError(f"Invalid parent_id for category {category_id}")
        names[category_id] = item
    if not names or payload["count"] != len(names):
        raise ValueError("Input count does not match unique category IDs")
    return names


def plan_updates(rows: list[dict], names: dict[int, dict]) -> list[dict]:
    if {row["id"] for row in rows} != set(names):
        raise ValueError("Catalog IDs changed; export and review the complete category tree again")
    updates = []
    for row in rows:
        item = names[row["id"]]
        if (row["name"] != item["name_original"] or row["slug"] != item["slug"]
                or row["parent_id"] != item["parent_id"]):
            raise ValueError(f"Source changed for category {row['id']}")
        values = {}
        for field in ("name_en",):
            previous = row[field]
            if previous is not None and previous != item[field]:
                raise ValueError(f"Existing {field} differs for category {row['id']}; review required")
            if previous != item[field]:
                values[field] = item[field]
        if values:
            updates.append({"id": row["id"], "previous": {
                "name_en": row["name_en"],
            }, "values": values})
    return updates


async def backfill(*, input_path: Path, report_path: Path, apply: bool) -> dict:
    raw_input = input_path.read_bytes()
    names = parse_names(json.loads(raw_input))
    input_sha256 = hashlib.sha256(raw_input).hexdigest()
    # Never clobber a previous rollback record.
    if report_path.exists():
        raise ValueError("Choose a new report path; existing reports are preserved")
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            if apply:
                # Freeze category tree writes for complete coverage, including concurrent inserts.
                await session.execute(text("LOCK TABLE categories IN SHARE ROW EXCLUSIVE MODE"))
            suffix = " FOR UPDATE" if apply else ""
            rows = [dict(row) for row in (await session.execute(text(
                "SELECT id, name, slug, parent_id, name_en FROM categories ORDER BY id" + suffix
            ))).mappings()]
            updates = plan_updates(rows, names)
            report = {"report_version": 1, "input_sha256": input_sha256, "entity": "categories",
                      "status": "prepared_apply" if apply else "dry_run", "count": len(rows),
                      "updates": updates, "unchanged": len(rows) - len(updates),
                      "records": [{"id": row["id"],
                                   "source": {key: row[key] for key in ("name", "slug", "parent_id")},
                                   "previous": {"name_en": row["name_en"]},
                                   "desired": {"name_en": names[row["id"]]["name_en"]}}
                                  for row in rows]}
            persist_report(report_path, report)
            if apply:
                for item in updates:
                    fields = item["values"]
                    assignments = ", ".join(f"{field} = :{field}" for field in fields)
                    await session.execute(text(f"UPDATE categories SET {assignments} WHERE id = :id"),
                                          {"id": item["id"], **fields})
        if apply:
            report["status"] = "committed"
            # Keep the pre-commit rollback record immutable, including on crash.
    return {"status": report["status"], "count": len(rows), "updated": len(updates),
            "unchanged": report["unchanged"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(backfill(input_path=args.input, report_path=args.report, apply=args.apply))))
