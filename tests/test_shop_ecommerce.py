from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.api.v1.routes.categories import (
    _category_tree,
    _facet_response,
    _filter_group_slug,
    _matches_selected_filters,
)
from app.api.v1.routes.products import _is_new_product, _product_order_clauses, build_shop_product_response, product_image_urls
from app.models.category import Category
from app.models.product import Product
from app.schemas.order import OrderCreate
from app.services.shop_promotion import ShopPriceResult
from app.services.product_popularity import (
    PopularitySignals,
    build_visitor_hash,
    calculate_popularity_results,
    is_refresh_due,
)


def _timestamp() -> datetime:
    return datetime.now(UTC)


def test_order_create_accepts_shop_checkout_payload() -> None:
    payload = OrderCreate.model_validate(
        {
            "firstName": "Іван",
            "lastName": "Чікунов",
            "phoneNumber": "+380990635700",
            "email": "user@example.com",
            "shippingCompany": "novaPost",
            "shippingMethod": "warehouse",
            "shippingCity": "Одеса",
            "shippingWarehouseNumber": "24",
            "paymentMethod": "cashOnDelivery",
            "promoCode": "WELCOME10",
            "items": [{"product_id": 1, "quantity": 2}],
        }
    )

    assert payload.resolved_customer_name == "Іван Чікунов"
    assert payload.resolved_customer_phone == "+380990635700"
    assert payload.resolved_customer_email == "user@example.com"
    assert payload.delivery_address == "Одеса, 24"
    assert payload.shipping_payload["shippingCompany"] == "novaPost"
    assert payload.promo_code == "WELCOME10"


def test_product_image_urls_uses_gallery_fallbacks_and_dedupes() -> None:
    product = Product(
        id=1,
        name="Trimmer",
        slug="trimmer",
        price=Decimal("100.00"),
        stock_quantity=5,
        is_active=True,
        image_url="https://cdn.example.com/main.jpg",
        attributes_json={
            "image_urls": ["https://cdn.example.com/main.jpg", "https://cdn.example.com/second.jpg"],
            "gallery": ["https://cdn.example.com/third.jpg"],
        },
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )

    assert product_image_urls(product) == [
        "https://cdn.example.com/main.jpg",
        "https://cdn.example.com/second.jpg",
        "https://cdn.example.com/third.jpg",
    ]


def test_product_is_new_for_three_calendar_months() -> None:
    created_at = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)

    assert _is_new_product(created_at, now=datetime(2026, 4, 30, 9, 59, tzinfo=UTC))
    assert not _is_new_product(created_at, now=datetime(2026, 4, 30, 10, 0, tzinfo=UTC))


def test_shop_product_can_be_new_and_discounted() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    product = Product(
        id=1,
        name="Clipper",
        slug="clipper",
        price=Decimal("80.00"),
        recommended_retail_price=Decimal("100.00"),
        stock_quantity=3,
        is_active=True,
        is_top=True,
        created_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        updated_at=now,
    )

    pricing = ShopPriceResult(
        base_price=Decimal("80.00"),
        price=Decimal("70.00"),
        discount_amount=Decimal("10.00"),
        discount_percent=Decimal("12.50"),
        promotion_id=7,
        promotion_name="Summer sale",
    )
    response = build_shop_product_response(product, categories={}, pricing=pricing, now=now)

    assert response.is_new is True
    assert response.is_top is True
    assert response.base_price == Decimal("80.00")
    assert response.price == Decimal("70.00")
    assert response.compare_at_price == Decimal("100.00")
    assert response.discount_percent == Decimal("30.00")
    assert response.promotion_name == "Summer sale"


def test_product_top_score_prioritizes_sales_and_limits_badges() -> None:
    results = calculate_popularity_results(
        {
            1: PopularitySignals(unique_views=200, paid_orders=8, purchased_units=12),
            2: PopularitySignals(unique_views=400, paid_orders=1, purchased_units=1),
            3: PopularitySignals(unique_views=60, paid_orders=3, purchased_units=4),
            4: PopularitySignals(unique_views=10, paid_orders=0, purchased_units=0),
        },
        top_fraction=0.1,
        max_top_products=8,
        min_unique_views=30,
        min_paid_orders=3,
    )

    assert results[1].is_top is True
    assert results[1].rank == 1
    assert sum(result.is_top for result in results.values()) == 1
    assert results[4].rank is None


def test_product_top_sort_alias_is_supported() -> None:
    clauses = _product_order_clauses("-is_top", None)

    assert len(clauses) == 3


def test_product_top_refresh_is_due_monthly() -> None:
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    assert not is_refresh_due(now - timedelta(days=29), now, 30)
    assert is_refresh_due(now - timedelta(days=30), now, 30)
    assert is_refresh_due(None, now, 30)


def test_product_view_hash_is_stable_and_prefers_customer_identity() -> None:
    anonymous = build_visitor_hash(
        secret="secret",
        customer_id=None,
        visitor_id="browser-123",
        client_host="127.0.0.1",
        user_agent="test",
    )
    authenticated = build_visitor_hash(
        secret="secret",
        customer_id=7,
        visitor_id="browser-123",
        client_host="127.0.0.1",
        user_agent="test",
    )

    assert len(anonymous) == 64
    assert anonymous != authenticated
    assert authenticated == build_visitor_hash(
        secret="secret",
        customer_id=7,
        visitor_id="another-browser",
        client_host="10.0.0.1",
        user_agent="another-agent",
    )


def test_public_category_tree_prunes_empty_branches() -> None:
    now = _timestamp()
    root = Category(id=1, name="Tools", slug="tools", is_active=True, parent_id=None, created_at=now, updated_at=now)
    drills = Category(id=2, name="Drills", slug="drills", is_active=True, parent_id=1, created_at=now, updated_at=now)
    empty = Category(id=3, name="Empty", slug="empty", is_active=True, parent_id=1, created_at=now, updated_at=now)

    tree = _category_tree([root, drills, empty], {2})

    assert [node.slug for node in tree] == ["tools"]
    assert [node.slug for node in tree[0].children] == ["drills"]


def test_public_category_tree_can_include_empty_branches_for_navigation() -> None:
    now = _timestamp()
    root = Category(id=1, name="Tools", slug="tools", is_active=True, parent_id=None, created_at=now, updated_at=now)
    drills = Category(id=2, name="Drills", slug="drills", is_active=True, parent_id=1, created_at=now, updated_at=now)
    empty = Category(id=3, name="Empty", slug="empty", is_active=True, parent_id=1, created_at=now, updated_at=now)

    tree = _category_tree([root, drills, empty], {2}, include_empty=True)

    assert [node.slug for node in tree] == ["tools"]
    assert [node.slug for node in tree[0].children] == ["drills", "empty"]


def test_catalog_filters_skip_import_metadata_attributes() -> None:
    product = Product(
        id=1,
        name="Clipper",
        slug="clipper",
        price=Decimal("120.00"),
        stock_quantity=3,
        is_active=True,
        attributes_json={
            "color": "Black",
            "source_url": "https://example.com/clipper",
            "parent_sku": "PARENT-1",
            "mpn": "MPN-1",
            "extra_category_paths": ["Tools/Clippers"],
            "source_added_at": "2026-07-09",
        },
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )

    filters = _facet_response([product])

    assert "color" in filters
    for key in ("source_url", "parent_sku", "mpn", "extra_category_paths", "source_added_at"):
        assert _filter_group_slug(key) not in filters


def test_catalog_matching_ignores_import_metadata_filters() -> None:
    product = Product(
        id=1,
        name="Clipper",
        slug="clipper",
        price=Decimal("120.00"),
        stock_quantity=3,
        is_active=True,
        attributes_json={
            "color": "Black",
            "source_url": "https://example.com/clipper",
            "parent_sku": "PARENT-1",
            "mpn": "MPN-1",
            "extra_category_paths": ["Tools/Clippers"],
            "source_added_at": "2026-07-09",
        },
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )
    selected = {
        _filter_group_slug("color"): {"black"},
        _filter_group_slug("source_url"): {"https-example-com-clipper"},
        _filter_group_slug("parent_sku"): {"parent-1"},
        _filter_group_slug("mpn"): {"mpn-1"},
        _filter_group_slug("extra_category_paths"): {"tools-clippers"},
        _filter_group_slug("source_added_at"): {"2026-07-09"},
    }

    assert _matches_selected_filters(product, selected)
