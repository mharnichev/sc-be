"""Update Reuzel profile from its official about page.

Revision ID: 0083_reuzel_official_copy
Revises: 0082_proraso_official_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0083_reuzel_official_copy"
down_revision = "0082_proraso_official_copy"
branch_labels = None
depends_on = None

DESCRIPTION_EN = (
    "Reuzel (pronounced “roo-zul”) is the Dutch word for lard. It is a professional "
    "styling and care brand made by barbers and used by everyone, combining quality, "
    "performance and the irreverent character of classic barbershop culture."
)
DESCRIPTION_UK = (
    "Reuzel (вимовляється «ру-зел») — нідерландське слово на позначення смальцю. "
    "Це професійний бренд засобів для укладання й догляду, створений барберами та "
    "доступний кожному. Він поєднує якість, ефективність і зухвалий характер класичної "
    "барберської культури."
)
HISTORY_EN = (
    "The Reuzel story began in Rotterdam around forty years ago amid rock-and-roll grit "
    "and punk rebellion. Barbers Leen and Bertus, known as The Bearded Bastard and The "
    "Bloody Butcher, wanted to make the perfect pomades for the counterculture they lived "
    "in. After extensive experimentation, they developed the early Red and Green formulas "
    "and drew on the heritage of traditional pomades to create Reuzel.\n\n"
    "The success of those first pomades led the founders to broaden the range: from products "
    "for a specific classic customer into a complete styling and care line for every hair "
    "type, texture and styling goal. The brand says that styles may change, but its attitude "
    "does not.\n\n"
    "Its name and pig logo reference pomade history: pig fat was once used to slick hair, "
    "while the French added apples, giving pomade its name. Modern Reuzel formulas contain "
    "no lard. The company is Leaping Bunny certified and does not test on animals; most "
    "products are vegan, while selected formulas contain beeswax."
)
HISTORY_UK = (
    "Історія Reuzel почалася в Роттердамі близько сорока років тому, в атмосфері рок-н-ролу "
    "та панк-бунту. Барбери Лін і Бертус, відомі як The Bearded Bastard і The Bloody Butcher, "
    "шукали ідеальну помаду для контркультури, частиною якої були самі. Після численних спроб "
    "вони створили ранні версії Red і Green та, вивчивши спадщину класичних помад, заклали "
    "основу Reuzel.\n\n"
    "Успіх перших помад спонукав засновників розвинути лінійку: від продуктів для конкретного "
    "класичного клієнта до комплексного догляду й стайлінгу для будь-якого типу, текстури "
    "волосся та стилістичної мети. Бренд декларує, що зі зміною стилів його ставлення "
    "залишається незмінним.\n\n"
    "Назва і логотип-свиня відсилають до історії помад: колись для укладання використовували "
    "свинячий жир, а французи додавали яблука — звідси назва pomade. У сучасних формулах "
    "Reuzel свинячого жиру немає. Компанія сертифікована Leaping Bunny, не тестує продукцію "
    "на тваринах; більшість її продуктів веганські, окремі формули містять бджолиний віск."
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
        .where(brands.c.slug == "reuzel")
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
        .where(brands.c.slug == "reuzel")
        .values(
            description_uk="Барберський бренд помад, засобів для укладання, догляду за волоссям і бородою, натхнений класичною культурою барбершопу.",
            description_en="A barbering brand of pomades, styling, hair-care and beard-care products inspired by classic barbershop culture.",
            history_uk="Reuzel розвиває виразну барберську айдентику та продукти для індивідуального стилю.",
            history_en="Reuzel develops a distinctive barbering identity and products for individual style.",
        )
    )
