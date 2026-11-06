from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app.api.v1.public.router import router
from app.core.database import get_db_session
from app.models.brand import Brand
from app.models.category import Category
from app.models.product import Product
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
from tests.test_segments_integration import anyio_backend, database  # noqa: F401

PATH = "/api/v1/public/products/by-slug/{}/recommendations"


@pytest.fixture
async def recommendations(database):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/public")

    async def session_dependency():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = session_dependency
    statements = []
    engine = database.kw["bind"].sync_engine

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://catalog.test") as client:
            yield client, statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def product(pid, **changes):
    data = dict(id=pid, name=f"Product {pid}", slug=f"p-{pid}", price=Decimal("100"),
                category_id=11, product_type="Hair Paste", stock_quantity=1, brand_id=1)
    return Product(**(data | changes))


async def categories_and_brands(session):
    session.add_all([
        Category(id=cid, name=f"Category {cid}", slug=f"c-{cid}", parent_id=parent, is_active=active)
        for cid, parent, active in [
            (10, None, True), (11, 10, True), (12, 10, True), (13, 10, False),
            (14, 13, True), (15, 11, True), (16, None, True), (17, None, True),
            (18, 10, True), (19, 10, True),
        ]
    ])
    session.add_all([Brand(id=i, name=f"Brand {i}", slug=f"b-{i}") for i in (1, 2)])
    await session.flush()


@pytest.mark.anyio
async def test_rank_contract_price_stock_variants_and_limit(database, recommendations):
    async with database() as session:
        await categories_and_brands(session)
        source = product(1, product_type="  HAIR\t Paste ", price=Decimal("400"),
                         reserved_quantity=1, variant_group_key="source", volume_ml=100)
        promo_winner = product(2, price=Decimal("300"), shop_price=Decimal("230"),
                               variant_group_key="candidate", volume_ml=100)
        session.add_all([
            source, promo_winner,
            product(3, price=Decimal("104")),
            product(4, price=Decimal("104"), brand_id=2, top_score=Decimal("0.9")),
            product(5, price=Decimal("104"), brand_id=2, top_score=Decimal("0.9")),
            product(6, price=Decimal("104"), brand_id=2, top_score=Decimal("0.8")),
            product(7, stock_quantity=0, allow_backorder=True),
            product(8, product_type="Shampoo"),
            product(9, product_type=None, price=Decimal("101")),
            product(10, category_id=12, variant_group_key="candidate", volume_ml=200),
            product(20, price=Decimal("102"), variant_group_key="candidate", volume_ml=300),
            product(21, variant_group_key="source", volume_ml=200),
            product(22, category_id=12, variant_group_key="source", volume_ml=300),
            product(23, is_active=False), product(24, reserved_quantity=1),
            product(25, stock_quantity=0),
        ])
        await session.flush()
        session.add_all([
            ShopPromotion(id=1, name="Public source price", trigger=ShopPromotionTrigger.automatic,
                          discount_type=ShopPromotionDiscountType.fixed_price,
                          discount_value=Decimal("100"), products=[source]),
            ShopPromotion(id=2, name="Public candidate price", trigger=ShopPromotionTrigger.automatic,
                          discount_type=ShopPromotionDiscountType.fixed_price,
                          discount_value=Decimal("101"), products=[promo_winner]),
        ])
        await session.commit()
    client, statements = recommendations
    response = await client.get(PATH.format("p-1"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items"}
    assert [item["id"] for item in body["items"]] == [2, 3, 4, 5, 6, 7, 8, 9]
    assert all(item["is_available_for_purchase"] and item["is_effectively_visible"] for item in body["items"])
    assert (body["items"][0]["price"], body["items"][0]["compare_at_price"], body["items"][0]["promotion_id"]) == (
        "101.00", "230.00", 2,
    )
    assert len(statements) <= 13
    print(f"ranked recommendation SQL SELECTs: {len(statements)}")
    catalog = (await client.get("/api/v1/public/products", params={"limit": 100})).json()
    assert body["items"][0] == next(item for item in catalog["items"] if item["id"] == 2)


@pytest.mark.anyio
async def test_sparse_categories_no_unrelated_matches_and_source_status(database, recommendations):
    async with database() as session:
        await categories_and_brands(session)
        session.add_all([
            product(1, category_id=12), product(2, category_id=12, product_type="Shampoo"),
            product(3, category_id=11, product_type=" hair   paste "),
            product(4, category_id=11, product_type="Shampoo", top_score=Decimal("0.99")),
            product(5, category_id=13), product(6, category_id=14), product(7, category_id=15),
            product(8, category_id=16), product(9, category_id=17),
            product(10, category_id=None), product(11, category_id=None),
            product(12, category_id=18, product_type=None),
            product(13, category_id=19, product_type="  "),
            product(14, category_id=12, is_active=False), product(15, category_id=11, reserved_quantity=1),
            product(16, category_id=11, product_type=None),
        ])
        await session.commit()
    client, _ = recommendations
    response = await client.get(PATH.format("p-1"))
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [2, 3]
    for pid in (8, 10, 12, 13):
        response = await client.get(PATH.format(f"p-{pid}"))
        assert response.status_code == 200
        assert response.json() == {"items": []}
    for slug in ("missing", "p-5", "p-6", "p-14"):
        assert (await client.get(PATH.format(slug))).status_code == 404


@pytest.mark.anyio
async def test_concurrent_edit_keeps_ranking_and_cards_in_one_snapshot(database, recommendations, monkeypatch):
    from sqlalchemy import update

    from app.services.shop_promotion import shop_promotion_service

    async with database() as session:
        await categories_and_brands(session)
        session.add_all([product(1), product(2)])
        await session.commit()
    price_products = shop_promotion_service.price_products

    async def change_catalog_during_pricing(*args, **kwargs):
        async with database() as writer:
            await writer.execute(update(Product).where(Product.id == 2).values(
                category_id=16, price=Decimal("200"), reserved_quantity=1,
            ))
            await writer.commit()
        return await price_products(*args, **kwargs)

    monkeypatch.setattr(shop_promotion_service, "price_products", change_catalog_during_pricing)
    client, _ = recommendations
    response = await client.get(PATH.format("p-1"))
    assert response.status_code == 200, response.text
    assert [(item["id"], item["category_id"], item["price"]) for item in response.json()["items"]] == [
        (2, 11, "100.00"),
    ]
    assert (await client.get(PATH.format("p-1"))).json() == {"items": []}
