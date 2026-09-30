"""Update DEPOT profile from its official concept page.

Revision ID: 0078_depot_official_copy
Revises: 0077_floid_official_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0078_depot_official_copy"
down_revision = "0077_floid_official_copy"
branch_labels = None
depends_on = None


DESCRIPTION_EN = (
    "There are many features that enhance a man’s style, and DEPOT®️ has the best solutions to "
    "enhance them all. Experience DEPOT and discover the complete range for male beauty routines. "
    "Because taking care of yourself means taking care of what makes you special.\n\n"
    "DEPOT. A man’s quality."
)
DESCRIPTION_UK = (
    "Є багато складових, що підкреслюють стиль чоловіка, і DEPOT®️ має найкращі рішення для кожної "
    "з них. Відчуйте DEPOT і відкрийте для себе повний асортимент для чоловічих ритуалів краси. "
    "Адже турбота про себе — це турбота про те, що робить вас особливими.\n\n"
    "DEPOT. Якість чоловіка."
)
HISTORY_EN = (
    "Discover the unique \"DEPOT EXPERIENCE\" at the barber's and at home.\n\n"
    "Specific products for hair, shaving and beard care, as well as body care and personal and home "
    "fragrances, to enhance a man’s style at all times.\n\n"
    "Refined ingredients, exclusive treatments and unique rituals that satisfy the needs of barbers and "
    "their most demanding customers. This is DEPOT.\n\n"
    "DEPOT is:\n• A complete range of professional products.\n• A unique experience both at home and "
    "in the barbershop.\n• The ideal partner for barbers who want to elevate and grow their business."
)
HISTORY_UK = (
    "Відкрийте для себе унікальний «досвід DEPOT» у барбершопі та вдома.\n\n"
    "Спеціальні продукти для волосся, гоління та догляду за бородою, а також догляду за тілом, "
    "особисті й домашні аромати, що допомагають підкреслити чоловічий стиль за будь-яких обставин.\n\n"
    "Вишукані інгредієнти, ексклюзивні процедури та унікальні ритуали, що задовольняють потреби "
    "барберів і їхніх найвибагливіших клієнтів. Це DEPOT.\n\n"
    "DEPOT — це:\n• повний асортимент професійних продуктів;\n• унікальний досвід і вдома, і в "
    "барбершопі;\n• ідеальний партнер для барберів, які хочуть підняти та розвинути свій бізнес."
)


def upgrade() -> None:
    brands = sa.table(
        "brands",
        sa.column("slug", sa.String()),
        sa.column("description_uk", sa.Text()),
        sa.column("description_en", sa.Text()),
        sa.column("history_uk", sa.Text()),
        sa.column("history_en", sa.Text()),
    )
    op.get_bind().execute(
        brands.update()
        .where(brands.c.slug == "depot")
        .values(
            description_uk=DESCRIPTION_UK,
            description_en=DESCRIPTION_EN,
            history_uk=HISTORY_UK,
            history_en=HISTORY_EN,
        )
    )


def downgrade() -> None:
    brands = sa.table(
        "brands",
        sa.column("slug", sa.String()),
        sa.column("description_uk", sa.Text()),
        sa.column("description_en", sa.Text()),
        sa.column("history_uk", sa.Text()),
        sa.column("history_en", sa.Text()),
    )
    op.get_bind().execute(
        brands.update()
        .where(brands.c.slug == "depot")
        .values(
            description_uk="Італійський професійний бренд чоловічого догляду для барбершопу та дому: волосся, гоління, борода, тіло й аромати.",
            description_en="An Italian professional male-grooming brand for the barbershop and home: hair, shaving, beard, body care and fragrances.",
            history_uk="DEPOT розвиває сучасну культуру барберингу й поєднує професійний сервіс із домашнім ритуалом догляду.",
            history_en="DEPOT develops contemporary barbering culture and connects professional service with home-care rituals.",
        )
    )
