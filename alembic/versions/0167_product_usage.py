"""Store product usage instructions separately from descriptions."""

from html import unescape
import re

from alembic import op
import sqlalchemy as sa

revision = "0167_product_usage"
down_revision = "0166_shop_mono_webhook"
branch_labels = None
depends_on = None

_SECTION = re.compile(
    r"<p>\s*<strong>\s*([^<>]+?)\s*</strong>\s*</p>", re.IGNORECASE
)
_LABELS = {
    "usage": ("спосіб застосування", "застосування", "спосіб використання"),
    "usage_en": ("how to use", "method of application", "method of use", "application"),
}


def _extract_usage(description: str | None, field: str) -> str | None:
    if not description:
        return None
    labels = _LABELS[field]
    sections = list(_SECTION.finditer(description))
    candidates = [
        description[section.end():sections[index + 1].start() if index + 1 < len(sections) else len(description)]
        for index, section in enumerate(sections)
        if unescape(section[1]).strip().rstrip(":").casefold() in labels
    ]
    if not candidates:
        inline = re.search(
            rf"<(?:strong|b)>\s*(?:{'|'.join(labels)})\s*:?\s*</(?:strong|b)>"
            r"(?:\s|&nbsp;|:|<br\s*/?>)*((?:(?!</p>|<strong>|<b>).)+)</p>",
            description,
            re.IGNORECASE | re.DOTALL,
        )
        if inline:
            candidates.append(f"<p>{inline[1]}</p>")
    for fragment in reversed(candidates):
        fragment = fragment.strip()
        if re.search(r"<a\b|https?://", fragment, re.IGNORECASE):
            continue
        if unescape(re.sub(r"<[^>]+>", "", fragment)).strip():
            return fragment
    return None


def upgrade() -> None:
    op.add_column("products", sa.Column("usage", sa.Text()))
    op.add_column("products", sa.Column("usage_en", sa.Text()))
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("products")}
    sources = {"usage": "description", "usage_en": "description_en"}
    for target, source in sources.items():
        if source not in columns:
            continue
        products = sa.table(
            "products", sa.column("id", sa.Integer()), sa.column(source, sa.Text()),
            sa.column(target, sa.Text()),
        )
        rows = connection.execute(sa.select(products.c.id, products.c[source])).all()
        values = [
            {"product_id": product_id, "instruction": instruction}
            for product_id, description in rows
            if (instruction := _extract_usage(description, target))
        ]
        if values:
            connection.execute(
                products.update().where(products.c.id == sa.bindparam("product_id"))
                .values({target: sa.bindparam("instruction")}),
                values,
            )


def downgrade() -> None:
    op.drop_column("products", "usage_en")
    op.drop_column("products", "usage")
