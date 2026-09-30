"""Populate separated product name fields; default is a dry run."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.product import Product
from app.utils.product_name_parts import split_product_name


async def backfill(*, apply: bool, force: bool, report_path: Path) -> dict:
    report: dict = {"apply": apply, "force": force, "updated": 0, "unchanged": 0, "matched_brand": 0, "fallback": 0, "items": []}
    async with AsyncSessionLocal() as session:
        products = list((await session.execute(select(Product).options(selectinload(Product.brand)).order_by(Product.id))).scalars())
        for product in products:
            parts = split_product_name(product.old_name or product.name, brand_name=product.brand.name if product.brand else None)
            report["matched_brand" if parts.matched_brand else "fallback"] += 1
            report["items"].append({"id": product.id, "old_name": parts.old_name, "model_name": parts.model_name, "product_type": parts.product_type, "package_size": parts.package_size, "matched_brand": parts.matched_brand})
            if apply:
                # Do not overwrite a correction made in the back office.  The
                # script is meant to enrich old records, not re-import them.
                values = {
                    field: value
                    for field, value in {
                        "old_name": parts.old_name,
                        "model_name": parts.model_name,
                        "product_type": parts.product_type,
                        "package_size": parts.package_size,
                    }.items()
                    if (force or getattr(product, field) is None) and value is not None
                }
                if values:
                    result = await session.execute(update(Product).where(Product.id == product.id).values(**values))
                    report["updated"] += result.rowcount
                else:
                    report["unchanged"] += 1
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if apply:
            await session.commit()
    return {key: len(value) if isinstance(value, list) else value for key, value in report.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true", help="Replace existing parser-generated values.")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(backfill(apply=args.apply, force=args.force, report_path=args.report)), ensure_ascii=False))
