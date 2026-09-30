"""Update The Bluebeards Revenge profile from its official about page.

Revision ID: 0085_bluebeards_official
Revises: 0084_slick_gorilla_official
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0085_bluebeards_official"
down_revision = "0084_slick_gorilla_official"
branch_labels = None
depends_on = None

DESCRIPTION_EN = (
    "The Bluebeards Revenge is a British men’s-grooming brand with barber-grade products for "
    "shaving, beard, hair, skin and body care. Its range is designed to provide professional "
    "results at home, while its refreshed identity pairs the brand’s distinctive character with "
    "a contemporary look."
)
DESCRIPTION_UK = (
    "The Bluebeards Revenge — британський бренд чоловічого грумінгу з барберською якістю для "
    "гоління, бороди, волосся, шкіри та тіла. Його асортимент створений, щоб давати "
    "професійний результат удома, а нова айдентика поєднує виразний характер бренду з "
    "сучасним дизайном."
)
HISTORY_EN = (
    "The Bluebeards Revenge was established in 2010 in the South West of England. It began "
    "with a rebellious spirit and a commitment to quality and sustainability. Since then, its "
    "products have been used by professional barbers and consumers around the world to achieve "
    "barber-grade results at home.\n\n"
    "The brand has refreshed its visual identity through a new logo, packaging and expanded "
    "range while retaining its established formulas for shaving, beard, hair, skin and body "
    "care. As part of this evolution, The Bluebeards Revenge highlights sustainability: its "
    "blue bottles are made from recycled materials, and the range is positioned as "
    "cruelty-free, vegan-friendly and paraben-free."
)
HISTORY_UK = (
    "The Bluebeards Revenge засновано 2010 року на південному заході Англії. Бренд виник із "
    "бунтарським духом і прагненням до якості та сталого розвитку. Відтоді його продуктами "
    "користуються професійні барбери та споживачі в різних країнах, щоб досягати барберського "
    "результату вдома.\n\n"
    "Бренд оновив візуальну айдентику: логотип, паковання та асортимент, зберігши перевірені "
    "формули для гоління, догляду за бородою, волоссям, шкірою й тілом. У межах цього оновлення "
    "The Bluebeards Revenge приділяє більше уваги екологічності: сині флакони виготовляють із "
    "перероблених матеріалів, а лінійка має cruelty-free, vegan-friendly та paraben-free "
    "позиціонування."
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
        .where(brands.c.slug == "the-bluebeards-revenge")
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
        .where(brands.c.slug == "the-bluebeards-revenge")
        .values(
            description_uk="Британський бренд чоловічого догляду для гоління, волосся, обличчя та тіла з виразною барберською айдентикою.",
            description_en="A British men's-care brand for shaving, hair, face and body with a distinctive barbering identity.",
            history_uk="The BlueBeards Revenge поєднує професійні засоби з грайливою барберською естетикою.",
            history_en="The BlueBeards Revenge combines professional products with a playful barbering aesthetic.",
        )
    )
