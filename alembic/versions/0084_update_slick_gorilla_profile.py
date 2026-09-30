"""Update Slick Gorilla profile from the manufacturer's official about page.

Revision ID: 0084_slick_gorilla_official
Revises: 0083_reuzel_official_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0084_slick_gorilla_official"
down_revision = "0083_reuzel_official_copy"
branch_labels = None
depends_on = None

DESCRIPTION_EN = (
    "Slick Gorilla is a men’s-grooming brand born from Leeds street and barbershop culture. "
    "It makes styling, hair-care and skin-care products, including its well-known volumising, "
    "matte hair-styling powder. Creativity, individuality and confident self-expression are at "
    "the heart of the brand."
)
DESCRIPTION_UK = (
    "Slick Gorilla — бренд чоловічого грумінгу, що виріс із вуличної та барберської культури "
    "Лідса. Він створює засоби для укладання, догляду за волоссям і шкірою, зокрема відомий "
    "стайлінг-пудрою для об’єму й матового фінішу. В основі бренду — творчість, "
    "індивідуальність і впевнене самовираження."
)
HISTORY_EN = (
    "Slick Gorilla was founded in 2016 on the streets and in the barbershops of Leeds. Its "
    "volumising hair powder became the product that put the brand on the map and, in the "
    "team’s view, changed the way people approach hair styling. From a local barbershop "
    "culture, the brand has grown into worldwide use while retaining that origin.\n\n"
    "Founders Ari and Ash built Slick Gorilla for bold, independent and creative people. The "
    "brand has expanded across styling, hair care and skin care while maintaining the idea "
    "that hair, fashion and personal style are parts of individuality."
)
HISTORY_UK = (
    "Slick Gorilla засновано 2016 року на вулицях і в барбершопах Лідса. Першим великим "
    "продуктом бренду стала пудра для об’єму, яка, за словами команди, змінила підхід до "
    "укладання волосся. Від локальної барберської культури бренд виріс до міжнародного "
    "використання, не втрачаючи зв’язку з власним походженням.\n\n"
    "Засновники Арі й Еш будували Slick Gorilla для сміливих, самостійних і творчих людей. "
    "Бренд розвиває стайлінг, догляд за волоссям та шкірою, підтримуючи ідею, що зачіска, "
    "мода й особистий стиль є частинами індивідуальності."
)


def _brands_table() -> sa.Table:
    return sa.table(
        "brands",
        sa.column("slug", sa.String()),
        sa.column("description_uk", sa.Text()),
        sa.column("description_en", sa.Text()),
        sa.column("history_uk", sa.Text()),
        sa.column("history_en", sa.Text()),
    )


def upgrade() -> None:
    brands = _brands_table()
    op.get_bind().execute(
        brands.update()
        .where(brands.c.slug == "slick-gorilla")
        .values(
            description_uk=DESCRIPTION_UK,
            description_en=DESCRIPTION_EN,
            history_uk=HISTORY_UK,
            history_en=HISTORY_EN,
        )
    )


def downgrade() -> None:
    brands = _brands_table()
    op.get_bind().execute(
        brands.update()
        .where(brands.c.slug == "slick-gorilla")
        .values(
            description_uk="Сучасний бренд чоловічого грумінгу, зосереджений на засобах для укладання та повсякденного догляду.",
            description_en="A contemporary men's-grooming brand focused on styling and everyday care.",
            history_uk="Slick Gorilla створює прості інструменти для самовираження через зачіску.",
            history_en="Slick Gorilla creates simple tools for self-expression through hair.",
        )
    )
