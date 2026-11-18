from datetime import UTC, datetime
from decimal import Decimal
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
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


@pytest.mark.anyio
async def test_compact_cards_preserve_public_catalog_behavior(http, database):
    async with database() as session:
        await session.execute(update(Product).where(Product.id == 101).values(
            description="<p>Full description</p>", ingredients="Ingredients", usage="Usage",
            stock_quantity=5, reserved_quantity=5, allow_backorder=True,
            attributes_json={"badges": ["top"], "Color": ["Red", "Blue"]},
        ))
        await session.commit()

    for path, params in [
        (CATALOG, {"ordering": "price_asc"}),
        (CATALOG, {"category": "catalog", "brand": "synthetic-brand", "type": "care",
                   "has_discount": True, "priceMin": "99", "priceMax": "100", "in_stock": False}),
        (CATALOG, {"limit": 1, "offset": 1}),
        ("/products", {"is_top": True, "ordering": "top"}),
        ("/products", {"in_stock": True}),
    ]:
        full, _ = await get(http, path, params=params)
        cards, _ = await get(http, path, params={**params, "view": "card"})
        assert {k: v for k, v in cards.items() if k != "items"} == {
            k: v for k, v in full.items() if k != "items"
        }
        assert [item["id"] for item in cards["items"]] == [item["id"] for item in full["items"]]
        for card, detail in zip(cards["items"], full["items"], strict=True):
            assert {"description", "ingredients", "usage", "volume_variants"}.isdisjoint(card)
            for key in ("brand", "category"):
                if detail[key]:
                    assert card[key] == {k: detail[key].get(k) for k in card[key]}
                else:
                    assert card[key] is None
            assert all(card[key] == detail[key] for key in card.keys() - {"brand", "category", "attributes_json"})
            if card["id"] == 103:
                assert card["attributes_json"] == {"image_urls": ["/media/suppressed.png"]}

    discounted, _ = await get(http, CATALOG, params={"view": "card", "has_discount": True})
    alpha, = discounted["items"]
    assert alpha["price"] == "100.00" and alpha["compare_at_price"] == "200.00"
    assert alpha["is_available_for_purchase"] is True
    assert alpha["images"][0] == "/media/alpha-preferred.webp"
    assert alpha["attributes_json"] == {"badges": ["top"]}
    detail, _ = await get(http, "/products/by-slug/alpha")
    assert detail["description"] == "<p>Full description</p>"


@pytest.mark.anyio
async def test_catalog_prices_overlapping_automatic_rules_before_facets_and_pagination(http, database):
    async with database() as session:
        root = await session.get(Category, 11)
        brand = await session.get(Brand, 31)
        alpha = await session.get(Product, 101)

        def rule(id, kind, value, **scope):
            return ShopPromotion(
                id=id, name=f"Rule {id}", trigger=ShopPromotionTrigger.automatic,
                discount_type=kind, discount_value=Decimal(value), **scope,
            )

        session.add_all([
            rule(302, ShopPromotionDiscountType.percent, "5", applies_to_all_products=True),
            rule(303, ShopPromotionDiscountType.fixed_amount, "10", brands=[brand]),
            rule(304, ShopPromotionDiscountType.fixed_amount, "25", categories=[root]),
            rule(305, ShopPromotionDiscountType.fixed_price, "1", categories=[root], include_subcategories=False),
            rule(306, ShopPromotionDiscountType.fixed_price, "100", products=[alpha], brands=[brand],
                 priority=50),
            rule(307, ShopPromotionDiscountType.fixed_price, "100", products=[alpha], priority=50),
            rule(308, ShopPromotionDiscountType.fixed_price, "1", applies_to_all_products=True,
                 ends_at=datetime(2020, 1, 1, tzinfo=UTC)),
        ])
        # Brands and product IDs are alternatives within a scope. Restrict rule
        # 306 to Alpha's brand so the other candidates exercise category rules.
        other_brand = Brand(id=32, name="Alternate", slug="alternate")
        session.add(other_brand)
        await session.flush()
        for pid in (102, 103, 107):
            await session.execute(update(Product).where(Product.id == pid).values(brand_id=32))
        await session.commit()

    page, _ = await get(http, CATALOG, params={
        "category": "catalog", "type": "care", "has_discount": True,
        "ordering": "price", "priceMin": "95", "priceMax": "125", "view": "card", "limit": 2,
    })
    assert page["total"] == 3
    assert page["price"] == {"min": "95.00", "max": "155.00"}
    assert counts(page, "type") == {"care": 3}
    assert counts(page, "brand") == {"alternate": 2, "synthetic-brand": 1}
    assert [(item["id"], item["price"], item["promotion_id"]) for item in page["items"]] == [
        (103, "95.00", 304), (101, "100.00", 306),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("path", [CATALOG, "/products"])
async def test_compact_cards_preserve_nonempty_english_product_and_category_names(http, database, path):
    async with database() as session:
        await session.execute(update(Product).where(Product.id == 101).values(
            name_en="English Alpha", product_type_en="English shampoo",
        ))
        await session.execute(update(Category).where(Category.id == 11).values(name_en="English catalog"))
        await session.execute(update(Category).where(Category.id == 12).values(name_en="English care"))
        await session.commit()

    full, _ = await get(http, path, params={"q": "CAT-101"})
    cards, _ = await get(http, path, params={"q": "CAT-101", "view": "card"})
    detail, = full["items"]
    card, = cards["items"]
    for product in (detail, card):
        assert product["name_en"] == "English Alpha"
        assert product["product_type_en"] == "English shampoo"
        assert product["category"]["name_en"] == "English care"
        assert [node["name_en"] for node in product["category_tree"]] == [
            "English catalog", "English care",
        ]
    assert card["category_tree"] == detail["category_tree"]
