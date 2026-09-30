"""Update Dapper Dan profile from its official about page.

Revision ID: 0079_dapper_dan_official
Revises: 0078_depot_official_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0079_dapper_dan_official"
down_revision = "0078_depot_official_copy"
branch_labels = None
depends_on = None


DESCRIPTION_EN = (
    "We firmly believe that with a handful of well selected and painstakingly developed products, "
    "there is no styling requirement that can not be met.\n\n"
    "We offer our range direct to our customers through this very website, and opportunities to become "
    "a reseller are only a phone call away. We pride ourselves on customer service and will do what we "
    "can, whenever we can in order to satisfy our customers' needs."
)
DESCRIPTION_UK = (
    "Ми твердо переконані, що за допомогою кількох ретельно відібраних і старанно розроблених "
    "продуктів можна задовольнити будь-яку потребу в укладанні.\n\n"
    "Ми пропонуємо наш асортимент напряму клієнтам на цьому сайті, а можливість стати реселером — "
    "лише за один телефонний дзвінок. Ми пишаємося сервісом і робимо все можливе, коли це можливо, "
    "щоб задовольнити потреби наших клієнтів."
)
HISTORY_EN = (
    "Dapper Dan was founded in Sheffield, England in 2011; born of frustration with the less than "
    "adequate products available to gentlemen to fulfil their styling needs.\n\n"
    "Our products were developed over a ten year period by professionals in the trade, determined to "
    "create a unique and compact styling range. In 2012, Dapper Dan Matt Paste was launched and quickly "
    "became the most versatile matt styler we had ever experienced.\n\n"
    "In 2014 we added a Deluxe Pomade and a Matt Clay to the range, and since then we've worked non-stop "
    "to release even more top quality products – from oil-based water-soluble Heavy Hold Pomade to 100% "
    "vegan friendly Vegetable Soap and more!"
)
HISTORY_UK = (
    "Dapper Dan було засновано в Шеффілді, Англія, у 2011 році через розчарування недостатньо "
    "якісними продуктами, доступними джентльменам для задоволення їхніх потреб в укладанні.\n\n"
    "Наші продукти розроблялися протягом десяти років професіоналами індустрії, які прагнули створити "
    "унікальний і компактний асортимент для укладання. У 2012 році було представлено Dapper Dan Matt "
    "Paste, яка швидко стала найуніверсальнішим матовим стайлером, який ми будь-коли бачили.\n\n"
    "У 2014 році ми додали до асортименту Deluxe Pomade та Matt Clay, а відтоді безперервно працюємо "
    "над випуском ще більшої кількості високоякісних продуктів — від олійної, але водорозчинної Heavy "
    "Hold Pomade до повністю веганського Vegetable Soap і не тільки."
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
        .where(brands.c.slug == "dapper-dan")
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
        .where(brands.c.slug == "dapper-dan")
        .values(
            description_uk="Британський бренд засобів для укладання та чоловічого грумінгу: помади, пасти, тоніки й засоби для гоління.",
            description_en="A British styling and men's-grooming brand offering pomades, pastes, tonics and shaving care.",
            history_uk="Dapper Dan поєднує засоби для укладання з барберською культурою класичних і сучасних зачісок.",
            history_en="Dapper Dan combines styling products with barbering culture for classic and contemporary looks.",
        )
    )
