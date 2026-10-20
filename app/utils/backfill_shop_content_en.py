"""Apply reviewed public English shop content atomically; default is dry run.

Only the reviewed 228 public IDs are in scope. Description source verification
compares sanitize_product_html(current stored source) with the exact public
artifact source; ingredients compare raw stored text. Originals are never edited.
An immutable, exclusive rollback report is durably written before any UPDATE.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import bindparam, text

from app.core.database import AsyncSessionLocal
from app.utils.backfill_english_report import persist_report
from app.utils.product_html import sanitize_product_html

FIELDS = ("description", "short_description", "ingredients")
ENGLISH_FIELDS = tuple(f"{field}_en" for field in FIELDS)
EXPECTED_COUNT = 228
SOURCE_POLICY = "description/short_description: sanitize_product_html(stored); ingredients: exact stored text"


def source_hash(source: dict) -> str:
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def parse_content(payload: dict, *, expected_count: int = EXPECTED_COUNT) -> dict[int, dict]:
    if not isinstance(payload, dict) or payload.get("failures") != []:
        raise ValueError("Input must have an empty failures list")
    items = payload.get("products")
    if not isinstance(items, list) or type(payload.get("count")) is not int:
        raise ValueError("Invalid product list/count")
    content = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid product record")
        product_id = item.get("id")
        if type(product_id) is not int or product_id <= 0 or product_id in content:
            raise ValueError("Product IDs must be unique positive integers")
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip() or slug != slug.strip():
            raise ValueError(f"Invalid slug for product {product_id}")
        source, translations = item.get("source"), item.get("translations")
        if not isinstance(source, dict) or set(source) != set(FIELDS):
            raise ValueError(f"Invalid source fields for product {product_id}")
        if not isinstance(translations, dict) or set(translations) != set(ENGLISH_FIELDS):
            raise ValueError(f"Invalid translation fields for product {product_id}")
        desired = {}
        for field in FIELDS:
            value, translated = source[field], translations[f"{field}_en"]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Invalid source {field} for product {product_id}")
            if field != "ingredients" and sanitize_product_html(value) != value:
                raise ValueError(f"Source {field} is not public canonical HTML for product {product_id}")
            if not value or not value.strip():
                if translated is not None:
                    raise ValueError(f"Empty source {field} must have null translation for product {product_id}")
                desired[f"{field}_en"] = None
            else:
                if not isinstance(translated, str) or not translated.strip():
                    raise ValueError(f"Missing translation {field} for product {product_id}")
                clean = translated if field == "ingredients" else sanitize_product_html(translated)
                if not clean or not clean.strip():
                    raise ValueError(f"Empty sanitized translation {field} for product {product_id}")
                desired[f"{field}_en"] = clean
        if item.get("source_sha256") != source_hash(source):
            raise ValueError(f"Source hash mismatch for product {product_id}")
        content[product_id] = {"id": product_id, "slug": slug, "source": dict(source), "translations": desired}
    if len(content) != expected_count or payload["count"] != expected_count:
        raise ValueError(f"Input must contain exactly {expected_count} unique reviewed product IDs")
    return content


def plan_updates(rows: list[dict], content: dict[int, dict]) -> list[dict]:
    if len(rows) != len(content) or {row["id"] for row in rows} != set(content):
        raise ValueError("Reviewed product IDs are missing or duplicated; export and review again")
    updates = []
    for row in rows:
        item = content[row["id"]]
        if row["slug"] != item["slug"]:
            raise ValueError(f"Source slug changed for product {row['id']}")
        for field in FIELDS:
            current = row[field] if field == "ingredients" else sanitize_product_html(row[field])
            if current != item["source"][field]:
                raise ValueError(f"Source {field} changed for product {row['id']}")
        values = {}
        for field in ENGLISH_FIELDS:
            desired = item["translations"][field]
            if row[field] is not None and row[field] != desired:
                raise ValueError(f"Existing {field} differs for product {row['id']}; review required")
            if row[field] != desired:
                values[field] = desired
        if values:
            updates.append({"id": row["id"], "previous": {field: row[field] for field in ENGLISH_FIELDS}, "values": values})
    return updates


async def backfill(*, input_path: Path, report_path: Path, apply: bool = False) -> dict:
    raw = input_path.read_bytes()
    content = parse_content(json.loads(raw))
    if report_path.exists():
        raise ValueError("Choose a new report path; existing reports are preserved")
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            if apply:
                await session.execute(text("LOCK TABLE products IN SHARE ROW EXCLUSIVE MODE"))
            fields = ", ".join(("id", "slug", *FIELDS, *ENGLISH_FIELDS))
            statement = text(f"SELECT {fields} FROM products WHERE id IN :ids ORDER BY id" + (" FOR UPDATE" if apply else ""))
            statement = statement.bindparams(bindparam("ids", expanding=True))
            rows = [dict(row) for row in (await session.execute(statement, {"ids": sorted(content)})).mappings()]
            updates = plan_updates(rows, content)
            report = {"report_version": 1, "entity": "products", "status": "prepared_apply" if apply else "dry_run",
                      "input_sha256": hashlib.sha256(raw).hexdigest(), "source_policy": SOURCE_POLICY,
                      "count": len(rows), "unchanged": len(rows) - len(updates), "updates": updates,
                      "records": [{"id": row["id"], "slug": row["slug"],
                                   "source_raw": {field: row[field] for field in FIELDS},
                                   "source_public": content[row["id"]]["source"],
                                   "previous": {field: row[field] for field in ENGLISH_FIELDS},
                                   "desired": content[row["id"]]["translations"]} for row in rows]}
            persist_report(report_path, report)
            if apply:
                for update in updates:
                    values = update["values"]
                    if not set(values).issubset(ENGLISH_FIELDS):
                        raise ValueError("Invalid update fields")
                    assignments = ", ".join(f"{field} = :{field}" for field in ENGLISH_FIELDS if field in values)
                    await session.execute(text(f"UPDATE products SET {assignments} WHERE id = :id"), {"id": update["id"], **values})
    return {"status": "committed" if apply else "dry_run", "count": len(rows), "updated": len(updates), "unchanged": report["unchanged"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(backfill(input_path=args.input, report_path=args.report, apply=args.apply))))
