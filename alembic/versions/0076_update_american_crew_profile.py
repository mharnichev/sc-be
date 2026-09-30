"""Update American Crew profile copy.

Revision ID: 0076_american_crew_copy
Revises: 0075_standard_issue_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0076_american_crew_copy"
down_revision = "0075_standard_issue_copy"
branch_labels = None
depends_on = None


DESCRIPTION_EN = (
    "American Crew® is more than just another product supplier. It's a landmark in the history of "
    "men's grooming. It's the leading salon brand created specifically for men and the stylists they "
    "trust. Every frame in our history reflects our commitment to the professional stylist and barber "
    "as we share in their quest to empower men through quality grooming practices and products.\n\n"
    "Today American Crew® is the leading professional men's grooming brand in the world. Men and their "
    "stylists count on us for the latest and best in hair, body, shave and styling products. And for "
    "more than 20 years, we've delivered"
)
DESCRIPTION_UK = (
    "American Crew® — це більше, ніж просто ще один постачальник продуктів. Це знакова сторінка в "
    "історії чоловічого грумінгу. Це провідний салонний бренд, створений спеціально для чоловіків і "
    "стилістів, яким вони довіряють. Кожен етап нашої історії відображає нашу відданість професійним "
    "стилістам і барберам, яких ми підтримуємо в їхньому прагненні надавати чоловікам упевненості "
    "завдяки якісним практикам і продуктам для догляду.\n\n"
    "Сьогодні American Crew® — провідний у світі професійний бренд чоловічого грумінгу. Чоловіки та "
    "їхні стилісти покладаються на нас, обираючи найновіші та найкращі засоби для волосся, тіла, "
    "гоління й укладання. Уже понад 20 років ми створюємо такі рішення."
)
HISTORY_EN = (
    "In 1994, a stylist named David Raccuglia foresaw a future when men would pay as much attention "
    "to their looks as anyone else. And he knew they wouldn't want to lose their masculinity in the process."
)
HISTORY_UK = (
    "У 1994 році стиліст Девід Раккулья передбачив час, коли чоловіки приділятимуть своїй зовнішності "
    "стільки ж уваги, як і будь-хто інший. Він також розумів, що вони не захочуть втрачати при цьому "
    "свою мужність."
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
        .where(brands.c.slug == "american-crew")
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
        .where(brands.c.slug == "american-crew")
        .values(
            description_uk="Професійний американський бренд чоловічого грумінгу для волосся, гоління, бороди й догляду за тілом.",
            description_en="A professional American men's-grooming brand for hair, shaving, beard and body care.",
            history_uk="American Crew заснував у 1994 році стиліст Девід Раккулья для чоловіків і барберів.",
            history_en="American Crew was founded in 1994 by stylist David Raccuglia for men and barbers.",
        )
    )
