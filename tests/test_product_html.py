from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import all_models  # noqa: F401 -- register ORM relationships
from app.models.product import Product
from app.schemas.common import PaginatedResponse
from app.schemas.product import (
    BackofficeProductResponse,
    ProductCreate,
    ProductResponse,
    ProductSearchResponse,
    ProductUpdate,
    PublicProductResponse,
    ShopProductResponse,
)
from app.schemas.shop import CartItemResponse, WishlistItemResponse
from app.services.catalog_assembly import build_shop_product_response
from app.services.catalog_visibility import VisibilityState
from app.utils.product_html import sanitize_product_html


NOW = datetime(2026, 10, 18, tzinfo=UTC)
RAW_HTML = '<p onclick="window.__xss=true">Опис <strong>товару</strong>.</p><img src=x onerror="window.__xss=true">'
SAFE_HTML = "<p>Опис <strong>товару</strong>.</p>"


@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("", ""),
    ("Зволожує шкіру — 100%", "Зволожує шкіру — 100%"),
    ("Олія & вода &amp; масло", "Олія &amp; вода &amp; масло"),
    ("&lt;img src=x onerror=alert(1)&gt;", "&lt;img src=x onerror=alert(1)&gt;"),
    ("<p>Опис <strong>товару</strong>, <em>догляд</em>.</p><ul><li>Один</li><li>Два</li></ul>",
     "<p>Опис <strong>товару</strong>, <em>догляд</em>.</p><ul><li>Один</li><li>Два</li></ul>"),
    ("<h3>Склад:</h3><p>Aqua.<br>Parfum.</p><ol><li>Нанести</li></ol>",
     "<h3>Склад:</h3><p>Aqua.<br>Parfum.</p><ol><li>Нанести</li></ol>"),
    (RAW_HTML, SAFE_HTML),
    ('<p style="color:red" id="login" class="overlay" onmouseover="alert(1)">Safe</p>', "<p>Safe</p>"),
    ('<!-- unsafe --><script>alert(1)</script><style>body{display:none}</style><p>Safe</p>', "<p>Safe</p>"),
    ('<iframe srcdoc="<script>alert(1)</script>">frame</iframe><object>object</object><embed src=x><p>Safe</p>', "<p>Safe</p>"),
    ('<svg><a xlink:href="javascript:alert(1)"><text>unsafe</text></a></svg><p>Safe</p>', "<p>Safe</p>"),
    ('<math><mtext><img src=x onerror=alert(1)></mtext></math><p>Safe</p>', "<p>Safe</p>"),
    ('<template><img src=x onerror=alert(1)></template><p>Safe</p>', "<p>Safe</p>"),
    ('<form action="javascript:alert(1)"><input autofocus onfocus="alert(1)"><button>Safe</button></form>', "Safe"),
    ('<p><b>Malformed<p onclick="alert(1)">still safe', "<p><b>Malformed</b></p><p><b>still safe</b></p>"),
    ('<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=alert(1)>">', ""),
])
def test_fragment_policy_and_idempotence(value: str | None, expected: str | None) -> None:
    cleaned = sanitize_product_html(value)
    assert cleaned == expected
    assert sanitize_product_html(cleaned) == cleaned


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "jav&#x61;script:alert(1)",
    "java&#9;script:alert(1)", "java&#10;script:alert(1)", "&#13;javascript:alert(1)",
    "data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;", "vbscript:msgbox(1)",
    "file:///etc/passwd", "blob:https://example.com/untrusted",
])
def test_executable_and_unapproved_link_schemes_are_removed(href: str) -> None:
    cleaned = sanitize_product_html(f'<a href="{href}" target="_blank" onclick="alert(1)">link</a>')
    assert cleaned == '<a rel="noopener noreferrer">link</a>'
    assert sanitize_product_html(cleaned) == cleaned


@pytest.mark.parametrize("href", [
    "https://example.com/product", "http://example.com/product", "mailto:shop@example.com",
    "/catalog?q=cream&amp;sort=name", "../catalog", "#ingredients", "//example.com/product",
])
def test_safe_links_keep_their_destination_without_unsafe_attributes(href: str) -> None:
    expected = f'<a href="{href}" title="Деталі" rel="noopener noreferrer">link</a>'
    assert sanitize_product_html(f'<a href="{href}" title="Деталі">link</a>') == expected
    assert sanitize_product_html(expected) == expected


def legacy_product() -> Product:
    return Product(
        id=1, name="Synthetic product", slug="synthetic-product", price=Decimal("100"),
        description=RAW_HTML, short_description=RAW_HTML, ingredients="Aqua, Parfum.",
        stock_quantity=1, reserved_quantity=0, allow_backorder=False, is_active=True,
        created_at=NOW, updated_at=NOW,
    )


@pytest.mark.parametrize("model", [ProductCreate, ProductUpdate])
def test_product_writes_sanitize_both_fields_without_changing_patch_semantics(model) -> None:
    payload = dict(description=RAW_HTML, short_description=RAW_HTML)
    if model is ProductCreate:
        payload.update(name="Synthetic product", slug="synthetic-product", price="100")
    result = model.model_validate(payload)
    assert result.description == result.short_description == SAFE_HTML
    assert model.model_validate(result.model_dump()).model_dump() == result.model_dump()
    if model is ProductUpdate:
        assert ProductUpdate(name="Renamed").model_dump(exclude_unset=True) == {"name": "Renamed"}
        assert ProductUpdate(description=None).model_dump(exclude_unset=True) == {"description": None}
        assert ProductUpdate(short_description=None).model_dump(exclude_unset=True) == {"short_description": None}


@pytest.mark.parametrize("model", [ProductResponse, PublicProductResponse, BackofficeProductResponse, ShopProductResponse])
def test_each_response_model_sanitizes_legacy_orm_without_mutating_it(model) -> None:
    product = legacy_product()
    payload = ProductResponse.model_validate(product).model_dump()
    # Deliberately restore raw legacy strings to verify every model's own boundary.
    payload.update(description=RAW_HTML, short_description=RAW_HTML)
    if model in (BackofficeProductResponse, ShopProductResponse):
        payload.update(is_effectively_visible=True, hidden_reason=None, images=[])
    if model is ShopProductResponse:
        payload.update(is_available_for_purchase=True, base_price=Decimal("100"))
    result = model.model_validate(payload)
    assert result.description == result.short_description == SAFE_HTML
    assert model.model_validate(result.model_dump()).model_dump() == result.model_dump()
    assert product.description == product.short_description == RAW_HTML


def test_actual_public_catalog_assembly_cleans_legacy_orm_before_serialization() -> None:
    product = legacy_product()
    result = build_shop_product_response(
        product, categories={}, visibility_state=VisibilityState(True, None),
        is_available_for_purchase=True, now=NOW,
    )
    assert result.description == result.short_description == SAFE_HTML
    assert result.ingredients == "Aqua, Parfum."
    assert product.description == product.short_description == RAW_HTML


@pytest.mark.parametrize("model", [CartItemResponse, WishlistItemResponse, ProductSearchResponse, PaginatedResponse[ShopProductResponse]])
def test_nested_private_cart_wishlist_search_and_catalog_apply_the_product_boundary(model) -> None:
    product = legacy_product()
    payload = build_shop_product_response(
        product, categories={}, visibility_state=VisibilityState(True, None),
        is_available_for_purchase=True, now=NOW,
    ).model_dump()
    payload.update(description=RAW_HTML, short_description=RAW_HTML)
    if model is ProductSearchResponse:
        result = model(products=[payload]).products[0]
    elif model in (CartItemResponse, WishlistItemResponse):
        result = model(
            id=1, product_id=1, quantity=1, product=payload,
            is_effectively_visible=True, is_available_for_purchase=True, hidden_reason=None,
            created_at=NOW, updated_at=NOW,
        ).product
    else:
        result = model(total=1, page=1, page_size=20, items=[payload]).items[0]
    assert result.description == result.short_description == SAFE_HTML


def test_fastapi_egress_of_legacy_orm_uses_sanitized_response_without_database_rewrite() -> None:
    product = legacy_product()
    app = FastAPI()

    @app.get("/legacy", response_model=PublicProductResponse)
    def get_legacy():
        return product

    with TestClient(app) as client:
        response = client.get("/legacy")
    assert response.status_code == 200
    assert response.json()["description"] == response.json()["short_description"] == SAFE_HTML
    assert product.description == product.short_description == RAW_HTML
