"""Copy labelled composition into products.ingredients; default is a dry run."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select, update

from app.core.database import AsyncSessionLocal
from app.models.product import Product
from app.utils.product_ingredients import extract_product_ingredients, ingredient_sections


async def backfill(*, apply: bool, report_path: Path) -> dict:
    report: dict = {"apply": apply, "updated": 0, "candidates": [], "missing": [], "ambiguous": [], "existing": 0}
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Product.id, Product.name, Product.description, Product.ingredients).order_by(Product.id)
        )).all()
        for row in rows:
            if row.ingredients is not None:
                report["existing"] += 1
                continue
            ingredients = extract_product_ingredients(row.description, product_name=row.name)
            if ingredients is None:
                key = "ambiguous" if ingredient_sections(row.description) else "missing"
                report[key].append({"id": row.id, "name": row.name})
                continue
            report["candidates"].append({"id": row.id, "name": row.name, "ingredients": ingredients})
            if apply:
                result = await session.execute(
                    update(Product)
                    .where(Product.id == row.id, Product.ingredients.is_(None), Product.description == row.description)
                    .values(ingredients=ingredients)
                )
                report["updated"] += result.rowcount
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if apply:
            await session.commit()
    return {key: len(value) if isinstance(value, list) else value for key, value in report.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(backfill(apply=args.apply, report_path=args.report)), ensure_ascii=False))
