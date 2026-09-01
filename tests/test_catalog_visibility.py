from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1.routes import categories as category_routes
from app.api.v1.routes import products as product_routes
from app.api.v1.routes.categories import _active_categories, _category_tree
from app.api.v1.routes.products import _backoffice_product_response, _active_product_stmt
from app.models.category import Category
from app.models.product import Product
from app.schemas.category import BackofficeCategoryResponse
from app.schemas.product import BackofficeProductResponse, ShopProductResponse
from app.services.catalog_visibility import CatalogVisibility, VisibilityState


NOW = datetime(2026, 8, 31, tzinfo=UTC)


def make_category(category_id: int, *, parent_id: int | None = None, active: bool = True) -> Category:
    return Category(
        id=category_id,
        name=f"Category {category_id}",
        slug=f"category-{category_id}",
        parent_id=parent_id,
        is_active=active,
        created_at=NOW,
        updated_at=NOW,
    )


def make_product(
    product_id: int,
    *,
    category_id: int | None = None,
    active: bool = True,
    stock_quantity: int = 1,
    availability_status: str | None = "in_stock",
) -> Product:
    return Product(
        id=product_id,
        name=f"Product {product_id}",
        slug=f"product-{product_id}",
        price=Decimal("10.00"),
        category_id=category_id,
        is_active=active,
        stock_quantity=stock_quantity,
        availability_status=availability_status,
        created_at=NOW,
        updated_at=NOW,
    )


def test_visibility_state_is_immutable() -> None:
    state = VisibilityState(is_effectively_visible=True, hidden_reason=None)

    with pytest.raises(FrozenInstanceError):
        state.is_effectively_visible = False  # type: ignore[misc]


def test_active_product_in_active_category_is_visible() -> None:
    context = CatalogVisibility.from_categories([make_category(1)])

    assert context.category_state(1) == VisibilityState(True, None)
    assert context.product_state(make_product(1, category_id=1)) == VisibilityState(True, None)


def test_product_and_category_reasons_have_correct_precedence() -> None:
    categories = [make_category(1, active=False), make_category(2, parent_id=1)]
    context = CatalogVisibility.from_categories(categories)

    assert context.product_state(make_product(1, category_id=2, active=False)) == VisibilityState(False, "product")
    assert context.product_state(make_product(2, category_id=1)) == VisibilityState(False, "category")
    assert context.product_state(make_product(3, category_id=2)) == VisibilityState(False, "parent_category")


def test_active_uncategorized_product_depends_only_on_own_status() -> None:
    context = CatalogVisibility.from_categories([make_category(1, active=False)])

    assert context.product_state(make_product(1)) == VisibilityState(True, None)
    assert context.product_state(make_product(2, active=False)) == VisibilityState(False, "product")


def test_category_reactivation_does_not_mutate_own_status_of_descendants_or_products() -> None:
    root = make_category(1, active=False)
    child = make_category(2, parent_id=1)
    hidden_product = make_product(1, category_id=2, active=False)

    hidden_context = CatalogVisibility.from_categories([root, child])
    assert hidden_context.product_state(hidden_product) == VisibilityState(False, "product")

    root.is_active = True
    visible_context = CatalogVisibility.from_categories([root, child])
    assert visible_context.category_state(2) == VisibilityState(True, None)
    assert hidden_product.is_active is False
    assert child.is_active is True
    assert visible_context.product_state(hidden_product) == VisibilityState(False, "product")


def test_out_of_stock_is_visible_but_not_available_for_purchase() -> None:
    context = CatalogVisibility.from_categories([make_category(1)])

    empty = make_product(1, category_id=1, stock_quantity=0)
    status_out = make_product(2, category_id=1, stock_quantity=5, availability_status="out_of_stock")
    hidden = make_product(3, category_id=1, active=False, stock_quantity=5)

    assert context.product_state(empty).is_effectively_visible is True
    assert context.is_available_for_purchase(empty) is False
    assert context.product_state(status_out).is_effectively_visible is True
    assert context.is_available_for_purchase(status_out) is False
    assert context.product_state(hidden).hidden_reason == "product"
    assert context.is_available_for_purchase(hidden) is False


def test_descendants_and_visible_category_ids_are_batch_derived() -> None:
    categories = [
        make_category(1),
        make_category(2, parent_id=1),
        make_category(3, parent_id=2, active=False),
        make_category(4, parent_id=1),
    ]
    context = CatalogVisibility.from_categories(categories)

    assert context.descendant_ids(1) == {1, 2, 3, 4}
    assert context.visible_category_ids() == {1, 2, 4}
    assert context.descendant_ids(99) == set()


def test_cycle_is_safe_and_does_not_make_recursive_resolution_loop() -> None:
    context = CatalogVisibility.from_categories([make_category(1, parent_id=2), make_category(2, parent_id=1)])

    assert context.descendant_ids(1) == {1, 2}
    assert context.category_state(1).is_effectively_visible is False
    assert context.category_state(2).is_effectively_visible is False


def test_sql_predicates_include_own_status_and_category_visibility() -> None:
    context = CatalogVisibility.from_categories([make_category(1)])
    statement = select(Product).where(context.visible_product_clause())
    compiled = str(statement)

    assert "products.is_active IS true" in compiled
    assert "products.category_id IS NULL" in compiled
    assert "products.category_id IN" in compiled
    assert "categories.id IN" in str(select(Category).where(context.visible_category_clause()))


class _ScalarResult:
    def __init__(self, values: list[Category]) -> None:
        self.values = values

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Category]:
        return self.values


class _OneExecuteSession:
    def __init__(self, categories: list[Category]) -> None:
        self.categories = categories
        self.execute_count = 0

    async def execute(self, _statement: object) -> _ScalarResult:
        self.execute_count += 1
        return _ScalarResult(self.categories)


def test_load_reads_categories_with_one_execute() -> None:
    session = _OneExecuteSession([make_category(1), make_category(2, parent_id=1)])

    context = asyncio.run(CatalogVisibility.load(session))  # type: ignore[arg-type]

    assert session.execute_count == 1
    assert context.visible_category_ids() == {1, 2}


def test_hidden_parent_is_excluded_from_product_list_and_category_tree() -> None:
    root = make_category(1, active=False)
    child = make_category(2, parent_id=1)
    context = CatalogVisibility.from_categories([root, child])

    product_statement = str(_active_product_stmt(context))
    tree = _category_tree(_active_categories(context), {2}, include_empty=True)

    assert "products.is_active IS true" in product_statement
    assert "products.category_id IS NULL" in product_statement
    assert tree == []


def test_backoffice_responses_require_computed_visibility_fields() -> None:
    product = make_product(1)
    category = make_category(1)

    with pytest.raises(ValidationError):
        BackofficeProductResponse.model_validate(product)
    with pytest.raises(ValidationError):
        BackofficeCategoryResponse.model_validate(category)
    with pytest.raises(ValidationError):
        ShopProductResponse.model_validate(product)


def test_backoffice_product_response_reports_parent_category_reason() -> None:
    context = CatalogVisibility.from_categories(
        [make_category(1, active=False), make_category(2, parent_id=1)]
    )
    response = _backoffice_product_response(make_product(1, category_id=2), context)

    assert response.is_effectively_visible is False
    assert response.hidden_reason == "parent_category"


class _RouteResult:
    def __init__(self, values: list[Any] | None = None, scalar: Any = None) -> None:
        self.values = values or []
        self.scalar = scalar

    def scalars(self) -> _RouteResult:
        return self

    def all(self) -> list[Any]:
        return self.values

    def scalar_one_or_none(self) -> Any:
        return self.scalar


class _RouteSession:
    def __init__(self, results: list[_RouteResult]) -> None:
        self.results = results
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _RouteResult:
        self.statements.append(statement)
        return self.results.pop(0)


@pytest.mark.anyio
async def test_hidden_product_returns_404_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = CatalogVisibility.from_categories(
        [make_category(1, active=False), make_category(2, parent_id=1)]
    )
    session = _RouteSession([_RouteResult(scalar=None)])

    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return context

    monkeypatch.setattr(product_routes.CatalogVisibility, "load", classmethod(load))

    with pytest.raises(HTTPException) as exc_info:
        await product_routes.get_product(7, session=session)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404
    assert "products.is_active IS true" in str(session.statements[0])
    assert "products.category_id IS NULL" in str(session.statements[0])

    slug_session = _RouteSession([_RouteResult(scalar=None)])
    with pytest.raises(HTTPException) as slug_exc_info:
        await product_routes.get_product_by_slug("hidden", session=slug_session)  # type: ignore[arg-type]

    assert slug_exc_info.value.status_code == 404
    assert "products.is_active IS true" in str(slug_session.statements[0])


@pytest.mark.anyio
async def test_search_uses_effective_product_and_category_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = CatalogVisibility.from_categories(
        [make_category(1), make_category(2, active=False), make_category(3, parent_id=2)]
    )
    session = _RouteSession([_RouteResult(), _RouteResult()])

    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return context

    async def no_prices(*_args: Any, **_kwargs: Any) -> dict[int, Any]:
        return {}

    monkeypatch.setattr(product_routes.CatalogVisibility, "load", classmethod(load))
    monkeypatch.setattr(product_routes.shop_promotion_service, "price_products", no_prices)

    response = await product_routes.search_products("clipper", 8, session=session)  # type: ignore[arg-type]

    assert response.products == []
    assert response.categories == []
    assert "products.category_id IN" in str(session.statements[0])
    assert "categories.id IN" in str(session.statements[1])


@pytest.mark.anyio
async def test_hidden_category_returns_404_from_direct_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = CatalogVisibility.from_categories([make_category(1, active=False)])
    session = _RouteSession([])

    async def load(_cls: Any, _session: Any) -> CatalogVisibility:
        return context

    monkeypatch.setattr(category_routes.CatalogVisibility, "load", classmethod(load))

    with pytest.raises(HTTPException) as exc_info:
        await category_routes.get_category(1, session=session)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404
    assert session.statements == []
