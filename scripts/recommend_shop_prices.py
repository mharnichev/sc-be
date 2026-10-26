"""Review all shop prices; optionally apply the conservative 9-ending policy.

Run: POSTGRES_HOST=127.0.0.1 python -m scripts.recommend_shop_prices --report PATH [--apply]
The exclusive report contains old/new prices for every product. Only shop_price
is updated. Supplier prices, recommendations, promotions and orders are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal, engine
from app.models.product import Product
from app.utils.shop_price_recommendation import recommend_shop_price


async def run(*, report_path: Path, apply: bool, expected_preview_path: Path | None = None) -> dict:
    expected = None
    if expected_preview_path is not None:
        expected = json.loads(expected_preview_path.read_text(encoding="utf-8"))
        if expected.get("mode") != "preview" or not isinstance(expected.get("products"), list):
            raise ValueError("Expected a preview report")
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                if apply:
                    await session.execute(text("SET LOCAL lock_timeout = '5s'"))
                    await session.execute(text("LOCK TABLE products IN SHARE ROW EXCLUSIVE MODE"))
                statement = select(Product).order_by(Product.id)
                if apply:
                    statement = statement.with_for_update()
                products = (await session.scalars(statement)).all()
                rows = []
                for product in products:
                    old = product.shop_price
                    suggested = recommend_shop_price(old) if old is not None else None
                    rows.append({
                        "id": product.id, "sku": product.sku, "name": product.name,
                        "base_price": str(product.price),
                        "old_shop_price": str(old) if old is not None else None,
                        "new_shop_price": str(suggested if suggested is not None else old) if old is not None else None,
                        "recommended_retail_price": str(product.recommended_retail_price) if product.recommended_retail_price is not None else None,
                        "changed": suggested is not None,
                    })
                    if apply and suggested is not None:
                        product.shop_price = suggested
                summary = {
                    "total": len(rows), "changed": sum(row["changed"] for row in rows),
                    "zero_or_missing": sum(product.shop_price is None or product.shop_price <= 0 for product in products),
                }
                if expected is not None and rows != expected["products"]:
                    raise ValueError("Catalogue changed since the reviewed preview; run preview again")
                report = {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "mode": "apply" if apply else "preview",
                    "policy": "nearest lower whole-UAH ending 9; maximum reduction 2%; never increase",
                    "summary": summary, "products": rows,
                }
                # Persist before commit: failure to save the audit rolls back the update.
                # The report describes intent; successful command exit confirms commit.
                with report_path.open("x", encoding="utf-8") as output:
                    report_path.chmod(0o600)
                    json.dump(report, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                directory_fd = os.open(report_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return summary
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the proposed shop_price changes")
    parser.add_argument("--report", type=Path, required=True, help="New JSON audit path; parent must exist")
    parser.add_argument("--expected-preview", type=Path, help="Reject catalogue drift from this reviewed preview")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(report_path=args.report, apply=args.apply, expected_preview_path=args.expected_preview))))
