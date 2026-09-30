"""Remove the deleted tattoo-care path from product import metadata.

Revision ID: 0090_tattoo_path_cleanup
Revises: 0089_tattoo_category_cleanup
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "0090_tattoo_path_cleanup"
down_revision = "0089_tattoo_category_cleanup"
branch_labels = None
depends_on = None

REMOVED_PATH = "КОСМЕТИКА/ДЛЯ ТІЛА/ДОГЛЯД ЗА ТАТУЮВАННЯМ"


def _clean_extra_paths(value: object) -> object:
    if not isinstance(value, dict):
        return value
    paths = value.get("extra_category_paths")
    if not isinstance(paths, list):
        return value
    cleaned = [path for path in paths if path != REMOVED_PATH]
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
    products = connection.execute(
        sa.text("SELECT id, attributes_json FROM products WHERE attributes_json IS NOT NULL")
    ).mappings()
    for product in products:
        cleaned = _clean_extra_paths(product["attributes_json"])
        if cleaned != product["attributes_json"]:
            connection.execute(
                sa.text(
                    "UPDATE products SET attributes_json = CAST(:attributes_json AS json), "
                    "updated_at = now() WHERE id = :id"
                ),
                {"id": product["id"], "attributes_json": json.dumps(cleaned, ensure_ascii=False)},
            )


def downgrade() -> None:
    # The removed import metadata path is intentionally not restored.
    pass
