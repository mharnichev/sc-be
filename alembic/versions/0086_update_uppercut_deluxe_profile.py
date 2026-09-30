"""Update Uppercut Deluxe profile from its official about page.

Revision ID: 0086_uppercut_official
Revises: 0085_bluebeards_official
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0086_uppercut_official"
down_revision = "0085_bluebeards_official"
branch_labels = None
depends_on = None

DESCRIPTION_EN = (
    "Uppercut Deluxe is an Australian men’s-grooming brand inspired by 1950s barbershop style "
    "and the legacy of Willy “Uppercut” O’Shea. It makes a core range of quality products for "
    "styling, hair washing, shaving and beard care without unnecessary salon embellishment."
)
DESCRIPTION_UK = (
    "Uppercut Deluxe — австралійський бренд чоловічого грумінгу, натхненний стилем барбершопів "
    "1950-х і постаттю Віллі «Uppercut» О’Ші. Він створює базову лінійку якісних засобів для "
    "укладання, миття волосся, гоління та догляду за бородою без зайвого салонного пафосу."
)
HISTORY_EN = (
    "The brand was created by childhood friends Luke Newman and Steve Purcell, barbers united "
    "by a love of the trade, surf and skate. They opened their own shop when barbering culture "
    "in Australia had not yet regained its current recognition, creating a welcoming place for "
    "men and a business that reflected their style.\n\n"
    "Uppercut Deluxe grew from frustration with products that did not fit their barbershop. "
    "Working with chemists, the founders spent more than a year refining formulas before "
    "launching their first product, Deluxe Pomade. The name honours Luke’s grandfather, boxer "
    "Willy O’Shea, known as Uppercut; his belief that a hard beginning leads to a good ending "
    "remains part of the brand’s ethos."
)
HISTORY_UK = (
    "Бренд створили друзі дитинства Люк Ньюман і Стів Перселл — барбери, яких об’єднували "
    "любов до ремесла, серфінгу й скейтбордингу. Вони відкрили власний барбершоп у час, коли "
    "барберська культура в Австралії ще не мала теперішнього визнання, щоб створити дружній "
    "простір для чоловіків і працювати у власному стилі.\n\n"
    "Uppercut Deluxe виник через невдоволення засобами, які не відповідали атмосфері їхнього "
    "барбершопу. Разом із хіміками засновники понад рік розробляли формули, перш ніж випустили "
    "перший продукт — Deluxe Pomade. Бренд названо на честь дідуся Люка, боксера Віллі О’Ші, "
    "відомого як Uppercut; його принцип про те, що важкий початок веде до гарного завершення, "
    "залишається частиною філософії бренду."
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
        .where(brands.c.slug == "uppercut-deluxe")
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
        .where(brands.c.slug == "uppercut-deluxe")
        .values(
            description_uk="Бренд чоловічого грумінгу для укладання, догляду за волоссям, гоління та бороди, натхненний класичною барберською культурою.",
            description_en="A men's-grooming brand for styling, hair care, shaving and beard care, inspired by classic barbering culture.",
            history_uk="Uppercut Deluxe розвиває функціональні засоби для щоденних укладок у традиції барбершопу.",
            history_en="Uppercut Deluxe develops functional products for everyday styling in the barbershop tradition.",
        )
    )
