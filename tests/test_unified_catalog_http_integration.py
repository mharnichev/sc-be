from sqlalchemy import update
import pytest

from app.models.brand import Brand
from app.models.category import Category
from app.models.product import Product
from tests.test_catalog_assembly_http_integration import catalog, get, http  # noqa: F401
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


CATALOG = "/products/catalog"


def counts(body, group):
    return {value["slug"]: value["count"] for value in body["filters"][group]["values"]}


@pytest.mark.anyio
async def test_contextual_counts_follow_the_same_filters_before_pagination(http, database):
    async with database() as session:
        session.add_all([
            Category(id=17, name="Shampoo", slug="shampoo", parent_id=12),
            Category(id=18, name="Tools", slug="tools", parent_id=11),
            Brand(id=32, name="Other", slug="other"),
        ])
        await session.flush()
        for pid, category_id, brand_id in [(101, 17, 31), (102, 17, 32), (107, 18, 32)]:
            await session.execute(update(Product).where(Product.id == pid).values(
                category_id=category_id, brand_id=brand_id,
            ))
        await session.commit()

    root, _ = await get(http, CATALOG)
    assert {item["id"] for item in root["items"]} == {101, 102, 103, 106, 107}
    assert counts(root, "type") == {"catalog": 4, "care": 3, "shampoo": 2, "tools": 1}

    params = [("category", "catalog"), ("type", "shampoo"), ("type", "tools"), ("brand", "other")]
    all_items, _ = await get(http, CATALOG, params=params)
    assert {item["id"] for item in all_items["items"]} == {102, 107}
    assert set(all_items["filters"]) == {"type", "brand"}
    assert counts(all_items, "type") == {"care": 1, "shampoo": 1, "tools": 1}
    assert counts(all_items, "brand") == {"other": 2, "synthetic-brand": 1}

    page, _ = await get(http, CATALOG, params=[*params, ("limit", "1"), ("offset", "1")])
    assert page["total"] == 2
    assert (page["page"], page["page_size"]) == (2, 1)
    assert page["items"] == all_items["items"][1:]
    assert page["filters"] == all_items["filters"]

    repeated_brands, _ = await get(http, CATALOG, params=[*params, ("brand", "synthetic-brand")])
    assert {item["id"] for item in repeated_brands["items"]} == {101, 102, 107}

    for group in ("type", "brand"):
        other_params = [(key, value) for key, value in params if key != group]
        for slug, expected in counts(page, group).items():
            option, _ = await get(http, CATALOG, params=[*other_params, (group, slug)])
            assert option["total"] == expected


@pytest.mark.anyio
async def test_catalog_preserves_stock_sale_top_search_and_effective_price(http, database):
    old, _ = await get(http, "/products", params={"ordering": "price_asc"})
    root, _ = await get(http, CATALOG, params={"ordering": "price_asc"})
    assert root["items"] == old["items"]
    assert [item["id"] for item in root["items"]] == [101, 103, 106, 107, 102]

    sale, _ = await get(http, CATALOG, params={
        "category": "catalog", "q": "CAT-", "in_stock": "true", "is_top": "true",
        "has_discount": "true", "priceMin": "99", "priceMax": "100",
    })
    assert [item["id"] for item in sale["items"]] == [101]
    assert sale["price"] == {"min": "100.00", "max": "100.00"}
    assert counts(sale, "brand") == {"synthetic-brand": 1}

    empty, _ = await get(http, CATALOG, params={"category": "catalog", "priceMax": "99"})
    assert empty["items"] == [] and empty["total"] == 0
    assert empty["filters"] == {}
    assert empty["price"] == {"min": "100.00", "max": "180.00"}

    async with database() as session:
        await session.execute(update(Product).where(Product.id == 101).values(reserved_quantity=5))
        await session.commit()
    unavailable, _ = await get(http, CATALOG, params={"in_stock": "false", "has_discount": "true"})
    assert [item["id"] for item in unavailable["items"]] == [101]

    client, _ = http
    assert (await client.get(CATALOG, params={"category": "hidden-child"})).status_code == 404
    assert (await client.get(CATALOG, params={"priceMin": "100", "priceMax": "99"})).status_code == 422


@pytest.mark.anyio
async def test_unknown_selected_type_can_be_removed_and_page_does_not_limit_facets(http):
    selected, _ = await get(http, CATALOG, params={"category": "catalog", "type": "unknown-type", "limit": 1})
    assert selected["total"] == 0 and selected["items"] == []
    assert counts(selected, "type") == {"care": 4}
    assert "unknown-type" not in counts(selected, "type")
    cleared, _ = await get(http, CATALOG, params={"category": "catalog", "limit": 1})
    assert cleared["total"] == 4 and len(cleared["items"]) == 1
    assert cleared["filters"]["type"] == selected["filters"]["type"]
    past_end, _ = await get(http, CATALOG, params={"category": "catalog", "limit": 1, "offset": 999})
    assert past_end["items"] == [] and past_end["total"] == 4
    assert past_end["filters"] == cleared["filters"]
