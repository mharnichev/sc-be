"""An isolated PostgreSQL verifies persistence and audit-before-commit safety."""
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models.product import Product
from scripts import recommend_shop_prices
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


@pytest.mark.anyio
async def test_preview_apply_and_audit_failure(database, monkeypatch, tmp_path):
    monkeypatch.setattr(recommend_shop_prices, "AsyncSessionLocal", database)
    async with database() as session:
        session.add_all([
            Product(name="Round", slug="round", price=Decimal("300"),
                    recommended_retail_price=Decimal("500"), shop_price=Decimal("500")),
            Product(name="Keep", slug="keep", price=Decimal("70"),
                    recommended_retail_price=Decimal("115"), shop_price=Decimal("115")),
            Product(name="Zero", slug="zero", price=Decimal("0"),
                    recommended_retail_price=Decimal("0"), shop_price=Decimal("0")),
        ])
        await session.commit()

    preview = tmp_path / "preview.json"
    assert await recommend_shop_prices.run(report_path=preview, apply=False) == {
        "total": 3, "changed": 1, "zero_or_missing": 1,
    }
    async with database() as session:
        assert await session.scalar(select(Product.shop_price).where(Product.slug == "round")) == Decimal("500")

    original_fsync = recommend_shop_prices.os.fsync
    def failing_sync(_):
        raise OSError("Simulated audit sync failure")
    monkeypatch.setattr(recommend_shop_prices.os, "fsync", failing_sync)
    with pytest.raises(OSError, match="audit sync failure"):
        await recommend_shop_prices.run(report_path=tmp_path / "failed-sync.json", apply=True)
    monkeypatch.setattr(recommend_shop_prices.os, "fsync", original_fsync)
    async with database() as session:
        assert await session.scalar(select(Product.shop_price).where(Product.slug == "round")) == Decimal("500")

    with pytest.raises(FileExistsError):
        await recommend_shop_prices.run(report_path=preview, apply=True)
    async with database() as session:
        assert await session.scalar(select(Product.shop_price).where(Product.slug == "round")) == Decimal("500")

    await recommend_shop_prices.run(report_path=tmp_path / "applied.json", apply=True, expected_preview_path=preview)
    async with database() as session:
        items = {p.slug: p for p in (await session.scalars(select(Product))).all()}
        assert items["round"].shop_price == Decimal("499")
        assert items["round"].price == Decimal("300")
        assert items["round"].recommended_retail_price == Decimal("500")
        assert items["keep"].shop_price == Decimal("115")
        assert items["zero"].shop_price == Decimal("0")
    assert (await recommend_shop_prices.run(report_path=tmp_path / "rerun.json", apply=True))["changed"] == 0
    with pytest.raises(ValueError, match="Catalogue changed"):
        await recommend_shop_prices.run(report_path=tmp_path / "stale.json", apply=True, expected_preview_path=preview)
    assert not (tmp_path / "stale.json").exists()


@pytest.mark.anyio
async def test_preview_rejects_new_products_and_commit_failure_rolls_back(database, monkeypatch, tmp_path):
    monkeypatch.setattr(recommend_shop_prices, "AsyncSessionLocal", database)
    async with database() as session:
        session.add(Product(name="First", slug="first", price=Decimal("300"), shop_price=Decimal("500"), stock_quantity=8, reserved_quantity=2))
        await session.commit()
    preview = tmp_path / "preview.json"
    await recommend_shop_prices.run(report_path=preview, apply=False)
    async with database() as session:
        session.add(Product(name="New", slug="new", price=Decimal("200"), shop_price=Decimal("555")))
        await session.commit()
    with pytest.raises(ValueError, match="Catalogue changed"):
        await recommend_shop_prices.run(report_path=tmp_path / "stale-new.json", apply=True, expected_preview_path=preview)

    def broken_session():
        session = database()
        def fail_commit(_):
            raise RuntimeError("Simulated commit failure")
        event.listen(session.sync_session, "before_commit", fail_commit)
        return session
    monkeypatch.setattr(recommend_shop_prices, "AsyncSessionLocal", broken_session)
    with pytest.raises(RuntimeError, match="commit failure"):
        await recommend_shop_prices.run(report_path=tmp_path / "uncommitted-intent.json", apply=True)
    async with database() as session:
        items = {p.slug: p for p in (await session.scalars(select(Product))).all()}
        assert items["first"].shop_price == Decimal("500")
        assert items["first"].stock_quantity == 8 and items["first"].reserved_quantity == 2
        assert items["new"].shop_price == Decimal("555")
