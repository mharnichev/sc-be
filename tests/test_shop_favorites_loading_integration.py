from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import insert

from app.models.product import Product
from app.models.shop import CustomerWishlistItem
from tests.test_catalog_assembly_http_integration import catalog, http  # noqa: F401
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


@pytest.mark.anyio
async def test_public_ids_and_account_wishlist_reach_saved_products_beyond_first_catalog_page(http, database):
    stamp = datetime(2021, 1, 1, tzinfo=UTC)
    async with database() as session:
        await session.execute(insert(Product), [
            dict(id=i, name=f"Product {i}", slug=f"favorite-{i}", price=Decimal("75"),
                 stock_quantity=1, created_at=stamp, updated_at=stamp)
            for i in range(1000, 1110)
        ])
        await session.execute(insert(CustomerWishlistItem), [
            dict(id=i, customer_id=21, product_id=i, created_at=stamp, updated_at=stamp)
            for i in range(1000, 1110)
        ])
        await session.commit()

    client, _ = http
    first_catalog = await client.get("/products", params={"page_size": 100})
    assert first_catalog.status_code == 200
    assert 1000 not in {item["id"] for item in first_catalog.json()["items"]}

    targeted = await client.get("/products", params={
        "ids": [1000, 1109, 1000, 105, 9999], "view": "card",
    })
    assert targeted.status_code == 200
    assert targeted.json()["total"] == 2
    assert [(item["id"], item["price"], item["is_available_for_purchase"])
            for item in targeted.json()["items"]] == [
        (1109, "75.00", True), (1000, "75.00", True),
    ]
    for invalid_ids in ([0], [2_147_483_648], list(range(1, 102))):
        invalid = await client.get("/products", params={"ids": invalid_ids})
        assert invalid.status_code == 422

    pages = [
        await client.get("/customers/wishlist", params={"page": page, "page_size": 100})
        for page in (1, 2)
    ]
    assert all(page.status_code == 200 for page in pages)
    first, second = (page.json()["items"] for page in pages)
    assert 1000 not in {item["product_id"] for item in first}
    assert 1000 in {item["product_id"] for item in second}
    assert 106 not in {item["product_id"] for item in first + second}  # Another customer's item.
    hidden = next(item for item in second if item["product_id"] == 105)
    assert hidden["is_effectively_visible"] is False
