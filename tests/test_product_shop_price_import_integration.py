"""Actual supplier XLSX imports preserve independently managed shop prices."""
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.models.product import Product
from app.utils import import_products as importer
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


@pytest.mark.anyio
async def test_xlsx_import_creates_shop_price_once_and_preserves_it_on_update(database, tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "AsyncSessionLocal", database)
    path = tmp_path / "supplier.xlsx"

    def workbook(rows):
        book = Workbook()
        book.active.append(["Артикул", "Название (UA)", "Цена", "РРЦ"])
        for row in rows:
            book.active.append(row)
        book.save(path)
        book.close()

    workbook([("RRP", "Synthetic RRP item", 100, 200),
              ("FALLBACK", "Synthetic fallback item", 75, None),
              ("ZERO", "Synthetic zero item", 0, None)])
    stats = await importer.import_products(path)
    assert (stats.products_created, stats.products_updated) == (3, 0)
    async with database() as session:
        products = {item.sku: item for item in (await session.scalars(select(Product))).all()}
        assert products["RRP"].shop_price == Decimal("200.00")
        assert products["FALLBACK"].shop_price == Decimal("75.00")
        assert products["ZERO"].shop_price == Decimal("0.00")
        products["RRP"].shop_price = Decimal("180.00")
        await session.commit()
    workbook([("RRP", "Synthetic RRP item", 110, 220),
              ("FALLBACK", "Synthetic fallback item", 85, 150),
              ("ZERO", "Synthetic zero item", 50, 90)])
    stats = await importer.import_products(path)
    assert (stats.products_created, stats.products_updated) == (0, 3)
    async with database() as session:
        products = {item.sku: item for item in (await session.scalars(select(Product))).all()}
        assert [(products[sku].price, products[sku].recommended_retail_price, products[sku].shop_price)
                for sku in ("RRP", "FALLBACK", "ZERO")] == [
            (Decimal("110.00"), Decimal("220.00"), Decimal("180.00")),
            (Decimal("85.00"), Decimal("150.00"), Decimal("75.00")),
            (Decimal("50.00"), Decimal("90.00"), Decimal("0.00")),
        ]
