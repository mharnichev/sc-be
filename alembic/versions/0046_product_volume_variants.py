"""link product variants by volume

Revision ID: 0046_product_volume_variants
Revises: 0045_daily_review_sms
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
import re
import unicodedata

from alembic import op
import sqlalchemy as sa


revision = "0046_product_volume_variants"
down_revision = "0045_daily_review_sms"
branch_labels = None
depends_on = None


VOLUME_PATTERN = re.compile(
    r"(?<![\d.,])(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|мл|l|л)(?![A-Za-zА-Яа-яІіЇїЄє])",
    re.IGNORECASE,
)


def _text(value: object | None) -> str:
    return "" if value is None else str(value).strip()


def _extract_volume_ml(name: str, size: str | None) -> int | None:
    for source in (_text(size), _text(name)):
        matches = list(VOLUME_PATTERN.finditer(source))
        if not matches:
            continue
        match = matches[-1]
        numeric_value = float(match.group("value").replace(",", "."))
        unit = match.group("unit").casefold()
        volume_ml = round(numeric_value * 1000) if unit in {"l", "л"} else round(numeric_value)
        return volume_ml if volume_ml > 0 else None
    return None


def _canonical_name(name: str) -> str:
    without_volume = VOLUME_PATTERN.sub(" ", unicodedata.normalize("NFKC", name))
    normalized = re.sub(r"[\s|/_,;:(){}\[\]-]+", " ", without_volume.casefold())
    return " ".join(normalized.split()).strip(" .")


def _attributes(value: object | None) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _group_key(brand_name: str, canonical_name: str) -> str:
    return sha256(f"{brand_name.casefold()}|{canonical_name}".encode("utf-8")).hexdigest()


def _backfill_product_volumes() -> None:
    products = sa.table(
        "products",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("attributes_json", sa.JSON()),
        sa.column("brand_id", sa.Integer()),
        sa.column("variant_group_key", sa.String()),
        sa.column("volume_ml", sa.Integer()),
    )
    brands = sa.table(
        "brands",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            products.c.id,
            products.c.name,
            products.c.attributes_json,
            brands.c.name.label("brand_name"),
        ).select_from(products.outerjoin(brands, products.c.brand_id == brands.c.id))
    ).mappings()

    candidates: dict[int, tuple[str, str, int]] = {}
    grouped: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for row in rows:
        name = _text(row["name"])
        attrs = _attributes(row["attributes_json"])
        volume_ml = _extract_volume_ml(name, _text(attrs.get("size")))
        canonical_name = _canonical_name(name) if volume_ml is not None else ""
        if not canonical_name or volume_ml is None:
            continue
        brand_name = _text(row["brand_name"])
        product_id = int(row["id"])
        candidates[product_id] = (brand_name, canonical_name, volume_ml)
        grouped[(brand_name.casefold(), canonical_name)].append((product_id, volume_ml))

    linked_groups: dict[int, str] = {}
    for (_brand, canonical_name), items in grouped.items():
        if len({volume_ml for _product_id, volume_ml in items}) < 2:
            continue
        brand_name = candidates[items[0][0]][0]
        group_key = _group_key(brand_name, canonical_name)
        linked_groups.update({product_id: group_key for product_id, _volume_ml in items})

    for product_id, (_brand_name, _canonical_product_name, volume_ml) in candidates.items():
        bind.execute(
            products.update()
            .where(products.c.id == product_id)
            .values(
                volume_ml=volume_ml,
                variant_group_key=linked_groups.get(product_id),
            )
        )


def upgrade() -> None:
    op.add_column("products", sa.Column("variant_group_key", sa.String(length=64), nullable=True))
    op.add_column("products", sa.Column("volume_ml", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_products_products_volume_ml_positive"),
        "products",
        "volume_ml IS NULL OR volume_ml > 0",
    )
    op.create_check_constraint(
        op.f("ck_products_products_variant_group_requires_volume"),
        "products",
        "variant_group_key IS NULL OR volume_ml IS NOT NULL",
    )
    op.create_index(
        "ix_products_variant_group_volume",
        "products",
        ["variant_group_key", "volume_ml"],
        unique=False,
    )
    _backfill_product_volumes()


def downgrade() -> None:
    op.drop_index("ix_products_variant_group_volume", table_name="products")
    op.drop_constraint(
        op.f("ck_products_products_variant_group_requires_volume"),
        "products",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_products_products_volume_ml_positive"),
        "products",
        type_="check",
    )
    op.drop_column("products", "volume_ml")
    op.drop_column("products", "variant_group_key")
