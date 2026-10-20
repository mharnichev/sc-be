"""Catalog HTTP characterization against explicitly isolated local PostgreSQL.

The shared database fixture never uses the application DATABASE_URL. Actual
routers run without an application lifespan, providers, or mocked catalog SQL.
Goldens are declared from synthetic inputs, not assembled by production helpers.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Integer, column, event, select, update, values

from app.api.v1.routes import categories, customers, products
from app.core.database import get_db_session
from app.dependencies.auth import get_current_customer
from app.models.brand import Brand
from app.models.category import Category
from app.models.customer import Customer
from app.models.product import Product
from app.models.shop import CustomerCartItem, CustomerWishlistItem, ProductImage, ProductImageVariant, ProductReview
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
from app.services.catalog_assembly import product_stock_order_clause
from tests.test_segments_integration import anyio_backend, database  # noqa: F401

AT = datetime(2020, 1, 1, tzinfo=UTC)
STAMP = "2020-01-01T00:00:00Z"
GALLERY = ["/media/alpha-preferred.webp", "/media/alpha-b.png", "/media/alpha-c.png", "/media/alpha-d.png"]
CATEGORY_INPUTS = [
    (11, "Catalog", "catalog", None, True),
    (12, "Care", "care", 11, True),
    (13, "Disabled", "disabled", None, False),
    (14, "Hidden parent", "hidden-parent", None, False),
    (15, "Hidden child", "hidden-child", 14, True),
    (16, "Empty", "empty", None, True),
]
PRODUCT_INPUTS = {
    101: dict(name="Alpha", slug="alpha", price="200.00", category_id=12, stock_quantity=5,
              model_name="Alpha model", variant_group_key="family", volume_ml=100, is_top=True,
              top_score="0.800000", attributes_json={"Color": ["Red", "Blue"]}, image_url="/media/unused.png"),
    102: dict(name="Beta", slug="beta", price="150.00", category_id=12, stock_quantity=0,
              variant_group_key="family", volume_ml=200, is_top=True, top_score="0.900000",
              attributes_json={"image_urls": ["/media/beta-a.png", "/media/beta-a.png", "/media/beta-b.png"]}),
    103: dict(name="Gamma", slug="gamma", price="120.00", category_id=12, stock_quantity=1,
              top_score="0.950000", attributes_json={"image_urls": ["/media/suppressed.png"]},
              image_url="/media/suppressed-primary.png"),
    104: dict(name="Delta", slug="delta", price="10.00", category_id=12, stock_quantity=1,
              is_active=False, variant_group_key="family", volume_ml=300),
    105: dict(name="Epsilon", slug="epsilon", price="5.00", category_id=15, stock_quantity=1),
    106: dict(name="Zeta", slug="zeta", price="150.00", category_id=None, stock_quantity=1,
              attributes_json={"image_urls": "/media/zeta.png"}),
    107: dict(name="Eta", slug="eta", price="180.00", category_id=12, stock_quantity=1,
              image_url="/media/eta.png"),
    108: dict(name="Theta", slug="theta", price="15.00", category_id=13, stock_quantity=1),
}


def category_golden(category_id):
    row = next(row for row in CATEGORY_INPUTS if row[0] == category_id)
    return dict(id=row[0], name=row[1], name_en=None, slug=row[2], parent_id=row[3], is_active=row[4],
                description=None, created_at=STAMP, updated_at=STAMP)


def product_golden(product_id, *, catalog=False):
    """Expected wire fields and intentionally distinct gallery/visibility policies."""
    raw = PRODUCT_INPUTS[product_id]
    images = {
        101: GALLERY, 102: ["/media/beta-a.png", "/media/beta-b.png"], 103: [],
        106: ["/media/zeta.png"], 107: ["/media/eta.png"],
    }.get(product_id, [])
    images = images[:3] if catalog else list(images)
    reason = {104: "product", 105: "parent_category", 108: "category"}.get(product_id)
    category_id = raw["category_id"]
    paths = {12: [11, 12], 15: [14, 15], 13: [13]}.get(category_id, [])
    result = dict(
        id=product_id, name=raw["name"], name_en=None, product_type_en=None, slug=raw["slug"], old_name=None,
        model_name=raw.get("model_name"), product_type=None, package_size=None,
        description=None, ingredients=None, short_description=None,
        price="100.00" if product_id == 101 else raw["price"],
        recommended_retail_price=None, sku=f"CAT-{product_id}",
        is_active=raw.get("is_active", True), image_url=images[0] if images else None,
        external_url=None, availability_status=None, attributes_json=raw.get("attributes_json"),
        variant_group_key=raw.get("variant_group_key"), volume_ml=raw.get("volume_ml"),
        brand_id=31, category_id=category_id,
        brand=dict(id=31, name="Synthetic brand", slug="synthetic-brand", description=None,
                   description_uk=None, description_en=None, history_uk=None, history_en=None,
                   website=None, logo_url=None, is_active=True, created_at=STAMP, updated_at=STAMP),
        category=category_golden(category_id) if category_id else None,
        created_at=STAMP, updated_at=STAMP, base_price=raw["price"], images=images,
        category_tree=[{key: category_golden(cid)[key] for key in ("id", "name", "name_en", "slug")} for cid in paths],
        compare_at_price="200.00" if product_id == 101 else None,
        discount_percent="50.00" if product_id == 101 else None,
        discount_amount="100.00" if product_id == 101 else "0.00",
        promotion_id=301 if product_id == 101 else None,
        promotion_name="Alpha half price" if product_id == 101 else None,
        promotion_code=None, is_new=False, is_top=raw.get("is_top", False),
        average_rating="3.67" if product_id == 101 else None,
        reviews_count=3 if product_id == 101 else 0, volume_variants=[],
        is_effectively_visible=reason is None, hidden_reason=reason,
        is_available_for_purchase=reason is None and raw["stock_quantity"] > 0,
    )
    return result


def detail_golden(product_id):
    result = product_golden(product_id)
    if product_id in (101, 102):
        result["volume_variants"] = [
            dict(id=pid, name=PRODUCT_INPUTS[pid].get("model_name", PRODUCT_INPUTS[pid]["name"]), name_en=None,
                 slug=PRODUCT_INPUTS[pid]["slug"], sku=f"CAT-{pid}", volume_ml=volume,
                 volume_label=f"{volume} мл", price=product_golden(pid)["price"],
                 base_price=PRODUCT_INPUTS[pid]["price"],
                 compare_at_price="200.00" if pid == 101 else None,
                 image_url=product_golden(pid)["image_url"], availability_status=None,
                 is_available=pid == 101)
            for pid, volume in [(101, 100), (102, 200)]
        ]
    return result


def page_golden(ids, *, total=None, page=1, page_size=20):
    return dict(total=len(ids) if total is None else total, page=page, page_size=page_size,
                items=[product_golden(pid, catalog=True) for pid in ids])


@pytest.fixture
async def catalog(database):
    async with database() as session:
        session.add_all([Category(id=cid, name=name, slug=slug, parent_id=parent, is_active=active,
                                 created_at=AT, updated_at=AT)
                         for cid, name, slug, parent, active in CATEGORY_INPUTS])
        session.add(Brand(id=31, name="Synthetic brand", slug="synthetic-brand", created_at=AT, updated_at=AT))
        session.add_all([Customer(id=cid, phone=f"+3805000000{cid}", name=f"Synthetic {cid}")
                         for cid in (21, 22, 23)])
        await session.flush()
        seeded_products = {}
        for pid, raw in PRODUCT_INPUTS.items():
            values = dict(raw)
            values["price"] = Decimal(values["price"])
            if "top_score" in values:
                values["top_score"] = Decimal(values["top_score"])
            product = Product(id=pid, sku=f"CAT-{pid}", brand_id=31, created_at=AT, updated_at=AT, **values)
            seeded_products[pid] = product
            session.add(product)
        await session.flush()
        # Deliberately unordered insertion, duplicate active source, and inactive source.
        session.add_all([
            ProductImage(id=201, product_id=101, image_url="/media/alpha-d.png", sort_order=4),
            ProductImage(id=202, product_id=101, image_url="/media/alpha-a.png", sort_order=0),
            ProductImage(id=203, product_id=101, image_url="/media/alpha-b.png", sort_order=1),
            ProductImage(id=204, product_id=101, image_url="/media/alpha-c.png", sort_order=3),
            ProductImage(id=205, product_id=101, image_url="/media/alpha-b.png", sort_order=2),
            ProductImage(id=206, product_id=101, image_url="/media/inactive.png", sort_order=-1, is_active=False),
            ProductImage(id=207, product_id=103, image_url="/media/gamma-inactive.png", sort_order=0, is_active=False),
        ])
        await session.flush()
        for vid, pid, source, output, status, preferred in [
            (211, 101, "/media/alpha-a.png", GALLERY[0], "succeeded", True),
            (212, 101, "/media/alpha-b.png", "/media/ignored-unpreferred.png", "succeeded", False),
            (213, 102, "/media/beta-a.png", "/media/ignored-failed.png", "failed", True),
        ]:
            session.add(ProductImageVariant(id=vid, product_id=pid, source_key=f"source-{vid}",
                                           source_url=source, source_fingerprint="a" * 64,
                                           preset="synthetic", recipe_version="synthetic", status=status,
                                           output_url=output, is_preferred=preferred))
        session.add(ShopPromotion(id=301, name="Alpha half price", trigger=ShopPromotionTrigger.automatic,
                                 discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal("50"),
                                 products=[seeded_products[101]]))
        session.add_all([ProductReview(product_id=101, customer_id=cid, rating=rating)
                         for cid, rating in [(21, 5), (22, 4), (23, 2)]])
        for item_id, pid in [(401, 101), (402, 102), (403, 104), (404, 105)]:
            stamp = AT.replace(day=406 - item_id)
            session.add(CustomerCartItem(id=item_id, customer_id=21, product_id=pid, quantity=2,
                                         created_at=stamp, updated_at=stamp))
            session.add(CustomerWishlistItem(id=item_id, customer_id=21, product_id=pid,
                                             created_at=stamp, updated_at=stamp))
        # Other customer's newer rows must never leak into totals or pages.
        session.add(CustomerCartItem(id=405, customer_id=22, product_id=106, created_at=AT.replace(day=10)))
        session.add(CustomerWishlistItem(id=405, customer_id=22, product_id=106, created_at=AT.replace(day=10)))
        await session.commit()


def create_app(database, *, customer_id=None):
    """Minimal HTTP harness around actual routers, with no lifespan startup."""
    app = FastAPI()
    app.include_router(products.public_router, prefix="/products")
    app.include_router(categories.public_router, prefix="/categories")
    app.include_router(customers.public_router, prefix="/customers")

    async def session_dependency():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = session_dependency
    if customer_id is not None:
        app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id=customer_id)
    return app


@pytest.fixture
async def http(database, catalog):
    selects = []
    engine = database.kw["bind"].sync_engine

    def record_select(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", record_select)
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(database, customer_id=21)),
                               base_url="http://catalog.test") as client:
            yield client, selects
    finally:
        event.remove(engine, "before_cursor_execute", record_select)


async def get(http, path, **kwargs):
    client, selects = http
    before = len(selects)
    response = await client.get(path, **kwargs)
    assert response.status_code == 200, response.text
    return response.json(), len(selects) - before


@pytest.mark.anyio
async def test_product_order_aliases_and_effective_discount_price_http_goldens(http, record_property):
    groups = [
        (["newest", "-created_at"], [107, 106, 103, 101, 102]),
        (["created_at"], [101, 103, 106, 107, 102]),
        (["price_asc", "price", "cheap", "cheaper"], [101, 103, 106, 107, 102]),
        (["price_desc", "-price", "expensive"], [107, 106, 103, 101, 102]),
        (["name", "name_asc"], [101, 107, 103, 106, 102]),
        (["-name", "name_desc"], [106, 103, 107, 101, 102]),
        (["top", "popular", "-popularity", "-is_top"], [101, 103, 107, 106, 102]),
    ]
    counts = {}
    for aliases, ids in groups:
        alias_counts = []
        for alias in aliases:
            body, count = await get(http, "/products", params={"sort": alias})
            ordered, ordering_count = await get(http, "/products", params={"ordering": alias})
            assert body == ordered == page_golden(ids)
            assert count == ordering_count
            alias_counts.append(count)
        assert len(set(alias_counts)) == 1
        counts[aliases[0]] = alias_counts[0]
    # Full-page and single-row requests use the same number of batched queries.
    one, one_count = await get(http, "/products", params={"sort": "name", "limit": 1})
    assert one == page_golden([101], total=5, page_size=1)
    assert one_count == counts["name"]
    record_property("catalog_select_counts", json.dumps(counts, sort_keys=True))


@pytest.mark.anyio
async def test_product_pagination_search_and_filter_aliases_serialize_identically(http):
    canonical, count = await get(http, "/products", params={"sort": "name", "page": 2, "page_size": 2})
    aliased, alias_count = await get(http, "/products", params={"sort": "name", "limit": 2, "offset": 2})
    assert canonical == aliased == page_golden([103, 106], total=5, page=2, page_size=2)
    assert count == alias_count
    precedence, _ = await get(http, "/products", params={"sort": "name", "ordering": "-name",
                                                        "page": 4, "page_size": 1, "limit": 2, "offset": 2})
    assert precedence == aliased
    search, _ = await get(http, "/products", params={"search": "Alpha"})
    query, _ = await get(http, "/products", params={"q": "Alpha", "search": "Beta"})
    assert search == query == page_golden([101])
    by_ids, _ = await get(http, "/products", params={"category_id": 12, "brand_id": 31, "sort": "name"})
    by_slugs, _ = await get(http, "/products", params={"category_slug": "care", "brand_slug": "synthetic-brand", "sort": "name"})
    assert by_ids == by_slugs == page_golden([101, 107, 103, 102])


@pytest.mark.anyio
async def test_category_products_price_aliases_descendants_and_attribute_filters(http, record_property):
    body, count = await get(http, "/categories/catalog/products", params={"sort": "cheap"})
    assert body == page_golden([101, 103, 107, 102])
    camel, _ = await get(http, "/categories/catalog/products", params={"priceMin": "100", "priceMax": "120"})
    snake, _ = await get(http, "/categories/catalog/products", params={"price_min": "100", "price_max": "120"})
    assert camel == snake == page_golden([103, 101])
    precedence, _ = await get(http, "/categories/catalog/products", params={"priceMin": "100", "priceMax": "120",
                                                                           "price_min": "151", "price_max": "200"})
    assert precedence == camel
    filtered, _ = await get(http, "/categories/catalog/products", params={"Color": "red,unknown"})
    assert filtered == page_golden([101])
    disjoint, _ = await get(http, "/categories/catalog/products", params={"Color": "green"})
    assert disjoint == page_golden([])
    paged, _ = await get(http, "/categories/catalog/products", params={"ordering": "price", "limit": 2, "offset": 2})
    assert paged == page_golden([107, 102], total=4, page=2, page_size=2)
    record_property("category_products_select_count", count)


@pytest.mark.anyio
async def test_detail_slug_id_volume_variants_and_gallery_wire_goldens(http, record_property):
    counts = {}
    for pid in (101, 102, 103, 106, 107):
        by_id, count = await get(http, f"/products/{pid}")
        by_slug, slug_count = await get(http, f"/products/by-slug/{PRODUCT_INPUTS[pid]['slug']}")
        assert by_id == by_slug == detail_golden(pid)
        assert count == slug_count
        counts[str(pid)] = count
    record_property("detail_select_counts", json.dumps(counts, sort_keys=True))


@pytest.mark.anyio
async def test_search_and_popularity_match_the_same_catalog_card(http):
    body, _ = await get(http, "/products/search", params={"q": "Alpha"})
    assert body == dict(suggestions=["Alpha model"], products=[product_golden(101, catalog=True)], categories=[])
    top, _ = await get(http, "/products", params={"is_top": "true", "sort": "popular"})
    assert top == page_golden([101, 102])
    category_top, _ = await get(http, "/categories/care/products", params={"is_top": "true", "ordering": "-is_top"})
    assert category_top == top
    # Nested categories participate in suggestions with the same serialized fields.
    care, _ = await get(http, "/products/search", params={"q": "Care"})
    assert care == dict(suggestions=["Care"], products=[], categories=[category_golden(12)])


@pytest.mark.anyio
async def test_category_list_visibility_tree_and_etag_http_contract(http, record_property):
    listed, count = await get(http, "/categories", params={"page_size": 2})
    assert listed == dict(total=3, page=1, page_size=2, items=[category_golden(12), category_golden(11)])
    rest, _ = await get(http, "/categories", params={"page": 2, "page_size": 2})
    assert rest == dict(total=3, page=2, page_size=2, items=[category_golden(16)])
    child = dict(category_golden(12), children=[])
    expected_tree = [dict(category_golden(11), children=[child])]
    client, selects = http
    before = len(selects)
    response = await client.get("/categories/tree")
    assert response.status_code == 200 and response.json() == expected_tree
    tree_count = len(selects) - before
    etag = response.headers["etag"]
    assert etag.startswith('W/"') and etag.endswith('"') and len(etag) == 68
    repeat = await client.get("/categories/tree", headers={"If-None-Match": etag})
    assert repeat.status_code == 304 and repeat.content == b"" and repeat.headers["etag"] == etag
    all_categories = await client.get("/categories/tree", params={"includeEmpty": "true"}, headers={"If-None-Match": etag})
    assert all_categories.status_code == 200
    assert all_categories.json() == expected_tree + [dict(category_golden(16), children=[])]
    assert all_categories.headers["etag"] != etag
    record_property("category_select_counts", json.dumps({"list": count, "tree": tree_count}))


@pytest.mark.anyio
@pytest.mark.parametrize("collection", ["cart", "wishlist"])
async def test_customer_collections_keep_full_cards_hidden_rows_order_and_pagination(http, collection, record_property):
    body, count = await get(http, f"/customers/{collection}")
    expected = []
    for item_id, pid in [(401, 101), (402, 102), (403, 104), (404, 105)]:
        product = product_golden(pid)
        stamp = f"2020-01-0{406 - item_id}T00:00:00Z"
        item = dict(id=item_id, product_id=pid, product=product, created_at=stamp, updated_at=stamp,
                    is_effectively_visible=product["is_effectively_visible"],
                    hidden_reason=product["hidden_reason"],
                    is_available_for_purchase=product["is_available_for_purchase"])
        if collection == "cart":
            item["quantity"] = 2
        expected.append(item)
    assert body == dict(total=4, page=1, page_size=20, items=expected)
    first, first_count = await get(http, f"/customers/{collection}", params={"page_size": 1})
    assert first == dict(total=4, page=1, page_size=1, items=expected[:1])
    assert first_count == count
    last, _ = await get(http, f"/customers/{collection}", params={"page": 2, "page_size": 2})
    assert last == dict(total=4, page=2, page_size=2, items=expected[2:])
    # Detail-only volume variants are omitted from customer cards; every other
    # serialized field, including full gallery/pricing/review stats, is identical.
    detail, _ = await get(http, "/products/101")
    normalized = deepcopy(detail)
    normalized["volume_variants"] = []
    assert first["items"][0]["product"] == normalized
    record_property(f"{collection}_select_count", count)


@pytest.mark.anyio
async def test_public_visibility_errors_validation_and_customer_auth_are_preserved(http, database):
    client, _ = http
    for pid in (104, 105, 108, 999):
        response = await client.get(f"/products/{pid}")
        assert response.status_code == 404 and response.json() == {"detail": "Product not found"}
    for slug in ("delta", "epsilon", "theta", "missing"):
        response = await client.get(f"/products/by-slug/{slug}")
        assert response.status_code == 404 and response.json() == {"detail": "Product not found"}
    for slug in ("disabled", "hidden-parent", "hidden-child", "missing"):
        response = await client.get(f"/categories/{slug}/products")
        assert response.status_code == 404 and response.json() == {"detail": "Category not found"}
    for cid in (13, 14, 15, 999):
        response = await client.get(f"/categories/{cid}")
        assert response.status_code == 404 and response.json() == {"detail": "Category not found"}
    for path in ("/products", "/categories/care/products"):
        response = await client.get(path, params={"sort": "unsupported"})
        assert response.status_code == 422 and response.json() == {"detail": "Invalid sort value"}
    for params, detail in [({"page": "-1"}, "'page' must be >= 1"),
                           ({"page_size": "101"}, "'page_size' must be between 1 and 100"),
                           ({"page": "invalid"}, "Invalid integer value for 'page'")]:
        response = await client.get("/products", params=params)
        assert response.status_code == 422 and response.json() == {"detail": detail}
    for collection in ("cart", "wishlist"):
        for pid in (104, 105, 108, 999):
            response = await client.post(f"/customers/{collection}", json={"product_id": pid})
            assert response.status_code == 404 and response.json() == {"detail": "Product not found"}
    async with AsyncClient(transport=ASGITransport(app=create_app(database)), base_url="http://catalog.test") as anonymous:
        for collection in ("cart", "wishlist"):
            response = await anonymous.get(f"/customers/{collection}")
            assert response.status_code == 401


@pytest.mark.anyio
async def test_reserved_stock_is_last_across_catalog_category_and_search(http, database):
    async with database() as session:
        await session.execute(update(Product).where(Product.id == 101).values(reserved_quantity=5))
        await session.commit()
    for path in ("/products", "/categories/catalog/products"):
        for params in ({}, {"sort": "price_asc"}, {"ordering": "price_desc"}, {"sort": "popular"}):
            body, _ = await get(http, path, params=params)
            ids = [item["id"] for item in body["items"]]
            assert set(ids[-2:]) == {101, 102}
            first, _ = await get(http, path, params={**params, "limit": 1})
            assert first["items"][0]["id"] not in {101, 102}
    # Every seeded product matches its SKU prefix; the limit must apply after stock priority.
    search, _ = await get(http, "/products/search", params={"q": "CAT-", "limit": 3})
    assert [item["id"] for item in search["products"]] == [107, 103, 106]


@pytest.mark.anyio
@pytest.mark.parametrize("sort", ["newest", "price_asc", "price_desc"])
@pytest.mark.parametrize("in_stock", [True, False])
async def test_postgres_stock_filter_total_and_pages(http, database, sort, in_stock):
    async with database() as session:
        await session.execute(update(Product).where(Product.id == 107).values(reserved_quantity=1))
        await session.commit()

    expected = {
        True: {"newest": [106, 103, 101], "price_asc": [101, 103, 106], "price_desc": [106, 103, 101]},
        False: {"newest": [107, 102], "price_asc": [102, 107], "price_desc": [107, 102]},
    }[in_stock][sort]
    ids = []
    for offset, expected_id in enumerate(expected):
        body, _ = await get(http, "/products", params={
            "in_stock": str(in_stock).lower(), "sort": sort, "limit": 1, "offset": offset,
        })
        assert body["total"] == len(expected)
        assert [item["id"] for item in body["items"]] == [expected_id]
        if expected_id == 101:
            assert body["items"][0]["price"] == "100.00"
        ids.extend(item["id"] for item in body["items"])
    assert ids == expected
    empty, _ = await get(http, "/products", params={
        "in_stock": str(in_stock).lower(), "sort": sort, "limit": 1, "offset": len(expected),
    })
    assert empty["total"] == len(expected)
    assert empty["items"] == []


@pytest.mark.anyio
async def test_postgres_null_reserved_expression_without_weakening_schema(database):
    # The real column is NOT NULL; nullable SQL values exercise legacy compatibility.
    rows = values(column("id", Integer), column("stock_quantity", Integer),
                  column("reserved_quantity", Integer), name="products").data([
        (1, 0, 0), (2, 4, 4), (3, 2, None), (4, 5, 2),
    ])
    statement = select(rows.c.id).order_by(product_stock_order_clause(), rows.c.id)
    async with database() as session:
        assert list((await session.execute(statement.limit(2))).scalars()) == [3, 4]
        assert list((await session.execute(statement.offset(2).limit(2))).scalars()) == [1, 2]
