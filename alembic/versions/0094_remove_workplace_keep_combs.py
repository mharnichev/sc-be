"""Remove Workplace catalog except professional combs and brushes.

Revision ID: 0094_workplace_cleanup
Revises: 0093_bonus_merch_cleanup
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "0094_workplace_cleanup"
down_revision = "0093_bonus_merch_cleanup"
branch_labels = None
depends_on = None


ROOT_CATEGORY_SLUG = "roboche-mistse"
KEPT_CATEGORY_SLUG = "roboche-mistse-profesiini-grebeni-ta-shchitki"
NEW_KEPT_CATEGORY_SLUG = "profesiini-grebeni-ta-shchitki"
ROOT_PATH = "РОБОЧЕ МІСЦЕ"
KEPT_CATEGORY_PATH = "РОБОЧЕ МІСЦЕ/ПРОФЕСІЙНІ ГРЕБЕНІ ТА ЩІТКИ"


def _descendant_ids(connection: sa.Connection, root_id: int) -> set[int]:
    category_ids = {root_id}
    frontier = {root_id}
    while frontier:
        children = set(
            connection.execute(
                sa.text("SELECT id FROM categories WHERE parent_id IN :parent_ids").bindparams(
                    sa.bindparam("parent_ids", expanding=True)
                ),
                {"parent_ids": tuple(frontier)},
            ).scalars()
        )
        frontier = children - category_ids
        category_ids.update(frontier)
    return category_ids


def _clean_workplace_paths(value: object) -> object:
    if not isinstance(value, dict):
        return value
    paths = value.get("extra_category_paths")
    if not isinstance(paths, list):
        return value

    cleaned: list[object] = []
    for path in paths:
        if not isinstance(path, str):
            cleaned.append(path)
            continue
        if path == ROOT_PATH:
            continue
        if path == KEPT_CATEGORY_PATH:
            cleaned.append(KEPT_CATEGORY_PATH.removeprefix(f"{ROOT_PATH}/"))
            continue
        if path.startswith(f"{KEPT_CATEGORY_PATH}/"):
            cleaned.append(path.removeprefix(f"{ROOT_PATH}/"))
            continue
        if path.startswith(f"{ROOT_PATH}/"):
            continue
        cleaned.append(path)

    if cleaned == paths:
        return value
    result = dict(value)
    if cleaned:
        result["extra_category_paths"] = cleaned
    else:
        result.pop("extra_category_paths", None)
    return result


def upgrade() -> None:
    connection = op.get_bind()
    root_id = connection.execute(
        sa.text("SELECT id FROM categories WHERE slug = :slug"),
        {"slug": ROOT_CATEGORY_SLUG},
    ).scalar_one_or_none()
    kept_id = connection.execute(
        sa.text("SELECT id FROM categories WHERE slug = :slug"),
        {"slug": KEPT_CATEGORY_SLUG},
    ).scalar_one_or_none()
    if root_id is None:
        return
    if kept_id is None or kept_id not in _descendant_ids(connection, root_id):
        raise RuntimeError("Cannot preserve the professional combs and brushes category")

    slug_conflict = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM categories WHERE slug = :slug AND id != :id)"),
        {"slug": NEW_KEPT_CATEGORY_SLUG, "id": kept_id},
    ).scalar_one()
    if slug_conflict:
        raise RuntimeError("Cannot detach the combs category: standalone slug is already in use")

    root_tree_ids = _descendant_ids(connection, root_id)
    kept_tree_ids = _descendant_ids(connection, kept_id)
    removed_category_ids = root_tree_ids - kept_tree_ids
    if removed_category_ids:
        has_order_history = connection.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM order_items oi "
                "JOIN products p ON p.id = oi.product_id "
                "WHERE p.category_id IN :category_ids)"
            ).bindparams(sa.bindparam("category_ids", expanding=True)),
            {"category_ids": tuple(removed_category_ids)},
        ).scalar_one()
        if has_order_history:
            raise RuntimeError("Cannot remove Workplace products: order history references them")

        connection.execute(
            sa.text("DELETE FROM products WHERE category_id IN :category_ids").bindparams(
                sa.bindparam("category_ids", expanding=True)
            ),
            {"category_ids": tuple(removed_category_ids)},
        )

    connection.execute(
        sa.text(
            "UPDATE categories SET parent_id = NULL, slug = :new_slug, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": kept_id, "new_slug": NEW_KEPT_CATEGORY_SLUG},
    )

    products = connection.execute(
        sa.text("SELECT id, attributes_json FROM products WHERE attributes_json IS NOT NULL")
    ).mappings()
    for product in products:
        cleaned = _clean_workplace_paths(product["attributes_json"])
        if cleaned != product["attributes_json"]:
            connection.execute(
                sa.text(
                    "UPDATE products SET attributes_json = CAST(:attributes_json AS json), "
                    "updated_at = now() WHERE id = :id"
                ),
                {"id": product["id"], "attributes_json": json.dumps(cleaned, ensure_ascii=False)},
            )

    kept_descendant_ids = kept_tree_ids - {kept_id}
    if kept_descendant_ids:
        connection.execute(
            sa.text("UPDATE categories SET slug = replace(slug, :old_prefix, :new_prefix) "
                    "WHERE id IN :category_ids").bindparams(
                sa.bindparam("category_ids", expanding=True)
            ),
            {
                "category_ids": tuple(kept_descendant_ids),
                "old_prefix": KEPT_CATEGORY_SLUG,
                "new_prefix": NEW_KEPT_CATEGORY_SLUG,
            },
        )

    if removed_category_ids:
        connection.execute(
            sa.text("DELETE FROM categories WHERE id IN :category_ids").bindparams(
                sa.bindparam("category_ids", expanding=True)
            ),
            {"category_ids": tuple(removed_category_ids)},
        )


def downgrade() -> None:
    # Deleted catalog products and original category relationships are not reconstructable.
    pass
