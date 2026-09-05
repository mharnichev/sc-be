"""Canonical supplier paths and reviewed assignments for the 2026-09 catalog cleanup."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def reviewed_category_assignments() -> dict[str, str]:
    groups = json.loads((Path(__file__).parent / 'data/catalog_category_assignments_v1.json').read_text())
    assignments: dict[str, str] = {}
    for path, skus in groups.items():
        for sku in skus:
            if sku in assignments:
                raise ValueError(f'Duplicate category assignment for SKU {sku}')
            assignments[sku] = path
    return assignments


def normalize_category_parts(category_path: str) -> list[str]:
    parts = [part.strip() for part in category_path.split('/') if part.strip()]
    if parts[:2] == ['НА ПРОДАЖ', 'ГРЕБНІ ТА ЩІТКИ']:
        parts[:2] = ['РОБОЧЕ МІСЦЕ', 'ПРОФЕСІЙНІ ГРЕБЕНІ ТА ЩІТКИ']
    elif parts[:1] == ['НА ПРОДАЖ']:
        parts[0] = 'КОСМЕТИКА'
    aliases = (
        ['КОСМЕТИКА', 'ДЛЯ ВОЛОССЯ', 'ШАМПУНІ'],
        ['КОСМЕТИКА', 'ДЛЯ БОРОДИ', 'ШАМПУНЬ'],
    )
    if any(parts[:3] == alias for alias in aliases):
        parts[:3] = ['КОСМЕТИКА', 'ДЛЯ ВОЛОССЯ', 'ШАМПУНЬ']
    return parts


def is_brand_category_path(path: str, brand_name: str | None = None) -> bool:
    parts = [part.strip().casefold() for part in path.split('/') if part.strip()]
    return bool(parts) and (
        parts[0] == 'бренди' or (brand_name is not None and parts[0] == brand_name.casefold())
    )


def resolve_import_category_path(
    category_path: str | None, *, sku: str, brand_name: str | None,
    extra_paths: list[str],
) -> str | None:
    if not category_path or not is_brand_category_path(category_path, brand_name):
        return '/'.join(normalize_category_parts(category_path)) if category_path else None
    reviewed = reviewed_category_assignments().get(sku)
    if reviewed:
        return reviewed
    candidates = {
        '/'.join(normalize_category_parts(path)) for path in extra_paths
        if not is_brand_category_path(path, brand_name)
    }
    # A root or conflicting sibling paths are not enough to classify a new item.
    leaves = {p for p in candidates if '/' in p and not any(q.startswith(p + '/') for q in candidates)}
    if len(leaves) == 1:
        return leaves.pop()
    raise ValueError(f'SKU {sku}: brand category requires a reviewed product category')
