from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from slugify import slugify

from app.api.v1.routes.products import (
    CATALOG_IMAGE_LIMIT,
    _product_order_clauses,
    _review_stats,
    build_shop_product_response,
)
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep, parse_optional_bool_query, parse_optional_int_query
from app.models.category import Category
from app.models.product import Product
from app.repositories.base import BaseRepository
from app.schemas.category import (
    BackofficeCategoryResponse,
    BackofficeCategoryTreeNode,
    CategoryCreate,
    CategoryResponse,
    CategoryTreeNode,
    CategoryUpdate,
)
from app.schemas.common import PaginatedResponse
from app.schemas.product import (
    CategoryFiltersResponse,
    FilterGroupResponse,
    FilterValueResponse,
    PriceRangeResponse,
    ShopProductResponse,
)
from app.services.shop_promotion import shop_promotion_service
from app.services.category import CategoryService
from app.services.catalog_visibility import CatalogVisibility

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Category)
service = CategoryService()

_KNOWN_CATEGORY_PRODUCTS_PARAMS = {
    "page",
    "page_size",
    "limit",
    "offset",
    "sort",
    "ordering",
    "priceMin",
    "priceMax",
    "price_min",
    "price_max",
    "is_top",
}
_EXCLUDED_ATTRIBUTE_KEYS = {
    "images",
    "image_urls",
    "gallery",
    "filters",
    "description",
    "short_description",
    "external_url",
    "source_url",
    "parent_sku",
    "mpn",
    "extra_category_paths",
    "source_added_at",
}


def _category_node(category: Category) -> CategoryTreeNode:
    return CategoryTreeNode(
        id=category.id,
        name=category.name,
        slug=category.slug,
        description=category.description,
        is_active=category.is_active,
        parent_id=category.parent_id,
        created_at=category.created_at,
        updated_at=category.updated_at,
        children=[],
    )


def _backoffice_category_response(
    category: Category,
    visibility: CatalogVisibility,
) -> BackofficeCategoryResponse:
    state = visibility.category_state(category.id)
    return BackofficeCategoryResponse(
        **CategoryResponse.model_validate(category).model_dump(),
        is_effectively_visible=state.is_effectively_visible,
        hidden_reason=state.hidden_reason,
    )


def _category_tree(
    categories: list[Category],
    product_category_ids: set[int],
    *,
    include_empty: bool = False,
) -> list[CategoryTreeNode]:
    nodes: dict[int, CategoryTreeNode] = {
        category.id: _category_node(category)
        for category in categories
    }
    children_by_parent: dict[int | None, list[Category]] = defaultdict(list)
    for category in categories:
        children_by_parent[category.parent_id].append(category)

    def build(category: Category) -> CategoryTreeNode | None:
        node = nodes[category.id].model_copy(update={"children": []})
        for child in children_by_parent.get(category.id, []):
            child_node = build(child)
            if child_node is not None:
                node.children.append(child_node)
        if include_empty or category.id in product_category_ids or node.children:
            return node
        return None

    roots: list[CategoryTreeNode] = []
    for category in children_by_parent.get(None, []):
        root = build(category)
        if root is not None:
            roots.append(root)
    return roots


def _active_categories(visibility: CatalogVisibility) -> list[Category]:
    return sorted(
        (visibility.categories_by_id[category_id] for category_id in visibility.visible_category_ids()),
        key=lambda category: (category.name, category.id),
    )


async def _active_product_category_ids(session: AsyncSession, visibility: CatalogVisibility) -> set[int]:
    return set(
        (
            await session.execute(
                select(Product.category_id)
                .where(visibility.visible_product_clause(), Product.category_id.is_not(None))
                .distinct()
            )
        )
        .scalars()
        .all()
    )


def _filter_group_slug(name: str) -> str:
    return slugify(name) or name.strip().lower().replace(" ", "-")


_EXCLUDED_FILTER_GROUP_SLUGS = {_filter_group_slug(key) for key in _EXCLUDED_ATTRIBUTE_KEYS}


def _iter_attribute_filters(product: Product) -> list[tuple[str, str, str, str]]:
    attrs = product.attributes_json if isinstance(product.attributes_json, dict) else {}
    source = attrs.get("filters") if isinstance(attrs.get("filters"), dict) else attrs
    values: list[tuple[str, str, str, str]] = []
    for key, value in source.items():
        group_slug = _filter_group_slug(str(key))
        if (
            key in _EXCLUDED_ATTRIBUTE_KEYS
            or group_slug in _EXCLUDED_FILTER_GROUP_SLUGS
            or value in (None, "", [])
        ):
            continue
        group_name = str(key)
        raw_values = value if isinstance(value, list) else [value]
        for raw_value in raw_values:
            if isinstance(raw_value, dict):
                display = raw_value.get("name") or raw_value.get("value") or raw_value.get("label")
                value_slug = raw_value.get("slug") or (slugify(str(display)) if display else None)
            else:
                display = str(raw_value)
                value_slug = slugify(display)
            if display and value_slug:
                values.append((group_slug, group_name, value_slug, str(display)))
    return values


def _product_filter_map(product: Product) -> dict[str, set[str]]:
    filters: dict[str, set[str]] = defaultdict(set)
    if product.brand:
        filters["brand"].add(product.brand.slug)
        filters["brand"].add(slugify(product.brand.name))
    for group_slug, _group_name, value_slug, value_name in _iter_attribute_filters(product):
        filters[group_slug].add(value_slug)
        filters[group_slug].add(slugify(value_name))
    return filters


def _selected_filters(request: Request) -> dict[str, set[str]]:
    selected: dict[str, set[str]] = defaultdict(set)
    for key, value in request.query_params.multi_items():
        group_slug = _filter_group_slug(key)
        if (
            key in _KNOWN_CATEGORY_PRODUCTS_PARAMS
            or group_slug in _EXCLUDED_FILTER_GROUP_SLUGS
            or not value
        ):
            continue
        for part in value.split(","):
            normalized = part.strip()
            if normalized:
                selected[group_slug].add(slugify(normalized) or normalized.lower())
    return selected


def _matches_selected_filters(product: Product, selected: dict[str, set[str]]) -> bool:
    if not selected:
        return True
    product_values = _product_filter_map(product)
    for group_slug, required_values in selected.items():
        if group_slug in _EXCLUDED_FILTER_GROUP_SLUGS:
            continue
        if not required_values.intersection(product_values.get(group_slug, set())):
            return False
    return True


def _facet_response(products: list[Product]) -> dict[str, FilterGroupResponse]:
    groups: dict[str, dict[str, Any]] = {
        "brand": {"name": "Бренд", "values": defaultdict(lambda: {"name": "", "count": 0})}
    }
    for product in products:
        if product.brand:
            item = groups["brand"]["values"][product.brand.slug]
            item["name"] = product.brand.name
            item["count"] += 1
        for group_slug, group_name, value_slug, value_name in _iter_attribute_filters(product):
            group = groups.setdefault(group_slug, {"name": group_name, "values": defaultdict(lambda: {"name": "", "count": 0})})
            item = group["values"][value_slug]
            item["name"] = value_name
            item["count"] += 1

    response: dict[str, FilterGroupResponse] = {}
    for group_slug, group in groups.items():
        values = [
            FilterValueResponse(slug=value_slug, name=value["name"], count=value["count"])
            for value_slug, value in sorted(group["values"].items(), key=lambda item: item[1]["name"])
            if value["count"] > 0
        ]
        if values:
            response[group_slug] = FilterGroupResponse(slug=group_slug, name=group["name"], values=values)
    return response


async def _category_products_stmt(
    session: AsyncSession,
    category_slug: str,
) -> tuple[Select[tuple[Product]], set[int], CatalogVisibility]:
    visibility = await CatalogVisibility.load(session)
    categories = _active_categories(visibility)
    category = next((item for item in categories if item.slug == category_slug), None)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    category_ids = visibility.descendant_ids(category.id)
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images))
        .where(visibility.visible_product_clause(), Product.category_id.in_(category_ids))
    )
    return stmt, category_ids, visibility


@public_router.get("/tree", response_model=list[CategoryTreeNode])
async def public_category_tree(
    request: Request,
    response: Response,
    include_empty: bool = Query(default=False, alias="includeEmpty"),
    session: AsyncSession = Depends(get_db_session),
) -> list[CategoryTreeNode] | Response:
    visibility = await CatalogVisibility.load(session)
    categories = _active_categories(visibility)
    product_category_ids = await _active_product_category_ids(session, visibility)
    tree = _category_tree(categories, product_category_ids, include_empty=include_empty)
    etag_payload = json.dumps(
        {
            "include_empty": include_empty,
            "categories": [
                {
                    "id": category.id,
                    "parent_id": category.parent_id,
                    "updated_at": category.updated_at.isoformat() if category.updated_at else None,
                    "has_products": category.id in product_category_ids,
                }
                for category in categories
            ],
        },
        sort_keys=True,
    )
    etag = f'W/"{sha256(etag_payload.encode("utf-8")).hexdigest()}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return tree


@public_router.get("/{category_slug}/products", response_model=PaginatedResponse[ShopProductResponse])
async def list_category_products(
    category_slug: str,
    request: Request,
    pagination: PaginationDep,
    sort: str | None = Query(default=None),
    ordering: str | None = Query(default=None),
    price_min: Decimal | None = Query(default=None, alias="priceMin"),
    price_max: Decimal | None = Query(default=None, alias="priceMax"),
    price_min_snake: Decimal | None = Query(default=None, alias="price_min"),
    price_max_snake: Decimal | None = Query(default=None, alias="price_max"),
    is_top: bool | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ShopProductResponse]:
    stmt, _category_ids, visibility = await _category_products_stmt(session, category_slug)
    if is_top is not None:
        stmt = stmt.where(Product.is_top.is_(is_top))
    stmt = stmt.order_by(*_product_order_clauses(sort, ordering))
    products = list((await session.execute(stmt)).scalars().all())
    prices = await shop_promotion_service.price_products(
        session,
        products,
        category_parents=visibility.category_parents(),
    )
    min_price = price_min if price_min is not None else price_min_snake
    max_price = price_max if price_max is not None else price_max_snake
    if min_price is not None:
        products = [product for product in products if prices[product.id].price >= min_price]
    if max_price is not None:
        products = [product for product in products if prices[product.id].price <= max_price]
    selected_filters = _selected_filters(request)
    products = [product for product in products if _matches_selected_filters(product, selected_filters)]

    sort_value = (sort or ordering or "newest").strip()
    if sort_value in {"price_asc", "price", "cheap", "cheaper"}:
        products.sort(key=lambda product: (prices[product.id].price, product.id))
    elif sort_value in {"price_desc", "-price", "expensive"}:
        products.sort(key=lambda product: (-prices[product.id].price, product.id))

    page_size = limit or pagination.page_size
    page_offset = offset if offset is not None else (pagination.page - 1) * pagination.page_size
    page = page_offset // page_size + 1
    paginated = products[page_offset : page_offset + page_size]
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [product.id for product in paginated])
    return PaginatedResponse[ShopProductResponse](
        total=len(products),
        page=page,
        page_size=page_size,
        items=[
            build_shop_product_response(
                product,
                categories=categories,
                image_limit=CATALOG_IMAGE_LIMIT,
                stats=stats,
                pricing=prices[product.id],
                visibility_state=visibility.product_state(product),
                is_available_for_purchase=visibility.is_available_for_purchase(product),
            )
            for product in paginated
        ],
    )


@public_router.get("/{category_slug}/filters", response_model=CategoryFiltersResponse)
async def category_filters(
    category_slug: str,
    is_top: bool | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> CategoryFiltersResponse:
    stmt, _category_ids, visibility = await _category_products_stmt(session, category_slug)
    if is_top is not None:
        stmt = stmt.where(Product.is_top.is_(is_top))
    products = list((await session.execute(stmt)).scalars().all())
    product_prices = await shop_promotion_service.price_products(
        session,
        products,
        category_parents=visibility.category_parents(),
    )
    prices = [product_prices[product.id].price for product in products]
    return CategoryFiltersResponse(
        price=PriceRangeResponse(
            min=min(prices) if prices else None,
            max=max(prices) if prices else None,
        ),
        filters=_facet_response(products),
    )


@public_router.get("", response_model=PaginatedResponse[CategoryResponse])
async def list_categories(
    pagination: PaginationDep,
    search: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CategoryResponse]:
    visibility = await CatalogVisibility.load(session)
    stmt = select(Category).order_by(Category.name.asc())
    stmt = stmt.where(visibility.visible_category_clause())
    if search:
        stmt = stmt.where(Category.name.ilike(f"%{search}%"))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[CategoryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[CategoryResponse.model_validate(item) for item in items],
    )


@public_router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(category_id: int, session: AsyncSession = Depends(get_db_session)) -> CategoryResponse:
    visibility = await CatalogVisibility.load(session)
    category = visibility.categories_by_id.get(category_id)
    if category is None or not visibility.category_state(category_id).is_effectively_visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return CategoryResponse.model_validate(category)


@backoffice_router.get("", response_model=PaginatedResponse[BackofficeCategoryResponse])
async def backoffice_list_categories(
    pagination: PaginationDep,
    is_active: str | None = Query(default=None),
    parent_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BackofficeCategoryResponse]:
    visibility = await CatalogVisibility.load(session)
    parsed_is_active = parse_optional_bool_query(is_active, "is_active")
    parsed_parent_id = parse_optional_int_query(parent_id, "parent_id")
    stmt = select(Category).order_by(Category.name.asc())
    if parsed_is_active is not None:
        stmt = stmt.where(Category.is_active.is_(parsed_is_active))
    if parsed_parent_id is not None:
        stmt = stmt.where(Category.parent_id == parsed_parent_id)
    if search:
        stmt = stmt.where(Category.name.ilike(f"%{search}%"))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[BackofficeCategoryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[_backoffice_category_response(item, visibility) for item in items],
    )


@backoffice_router.get("/tree", response_model=list[BackofficeCategoryTreeNode])
async def backoffice_category_tree(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[BackofficeCategoryTreeNode]:
    visibility = await CatalogVisibility.load(session)
    categories = sorted(
        visibility.categories_by_id.values(),
        key=lambda category: (category.name, category.id),
    )

    nodes: dict[int, BackofficeCategoryTreeNode] = {}
    for category in categories:
        response = _backoffice_category_response(category, visibility)
        nodes[category.id] = BackofficeCategoryTreeNode(
            **response.model_dump(),
            children=[],
        )
    roots: list[BackofficeCategoryTreeNode] = []

    for category in categories:
        node = nodes[category.id]
        if category.parent_id and category.parent_id in nodes:
            parent = nodes[category.parent_id]
            parent.children.append(node)
        else:
            roots.append(node)

    return roots


@backoffice_router.get("/{category_id}", response_model=BackofficeCategoryResponse)
async def backoffice_get_category(
    category_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeCategoryResponse:
    visibility = await CatalogVisibility.load(session)
    category = visibility.categories_by_id.get(category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return _backoffice_category_response(category, visibility)


@backoffice_router.post("", response_model=BackofficeCategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeCategoryResponse:
    category = await service.create_category(session, payload.model_dump())
    visibility = await CatalogVisibility.load(session)
    return _backoffice_category_response(category, visibility)


@backoffice_router.put("/{category_id}", response_model=BackofficeCategoryResponse)
async def update_category(
    category_id: int,
    payload: CategoryUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeCategoryResponse:
    category = await repo.get(session, category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    updated = await service.update_category(session, category, payload.model_dump(exclude_unset=True))
    visibility = await CatalogVisibility.load(session)
    return _backoffice_category_response(updated, visibility)


@backoffice_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    category = await repo.get(session, category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    await service.delete_category(session, category)
