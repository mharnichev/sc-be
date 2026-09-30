"""Update Morgan's Pomade profile from its official history page.

Revision ID: 0087_morgans_official
Revises: 0086_uppercut_official
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0087_morgans_official"
down_revision = "0086_uppercut_official"
branch_labels = None
depends_on = None

DESCRIPTION_EN = (
    "Morgan’s Pomade is a British maker of professional hair, styling, beard, shaving and "
    "skin-care products. The brand combines the classic sleek look with modern formulas, "
    "carefully sourced ingredients and technology; its products are used by barbers and "
    "hairdressers around the world."
)
DESCRIPTION_UK = (
    "Morgan’s Pomade — британський виробник професійних засобів для волосся, укладання, "
    "бороди, гоління та догляду за шкірою. Бренд поєднує класичний гладкий образ із сучасними "
    "формулами, ретельно відібраною сировиною та технологіями; його продукцію використовують "
    "барбери й перукарі в різних країнах."
)
HISTORY_EN = (
    "Morgan’s began in the family kitchen, where the first shampoo, labels, packaging and "
    "distribution were all handled by hand. The first product was Marie Antoinette Eucalyptus "
    "Egg Julep Shampoo, followed a year or two later by Morgan’s Pomade. Fashionable men’s "
    "hairdressers in London’s West End were the first stockists; demand moved production to a "
    "small Highgate factory and later to larger premises.\n\n"
    "Despite raw-material difficulties during both world wars, Morgan’s continued building its "
    "export business: India became its largest market during the wartime period, with West "
    "Africa and Nigeria important after the war. The brand bought a Hornsey Rise factory in "
    "1935 and later built dedicated manufacturing in Whitstable, Kent. From the 1970s, the "
    "range expanded into shampoos and styling products, private-label offerings for barbers "
    "and hairdressers, professional hair care and the Men’s Retro Barber range. Today Morgan’s "
    "is distributed in more than 50 export markets."
)
HISTORY_UK = (
    "Історія Morgan’s почалася на домашній кухні родини: перші шампунь, етикетки, паковання "
    "та поставки робили вручну. Першим продуктом став Marie Antoinette Eucalyptus Egg Julep "
    "Shampoo, а через рік або два з’явилася Morgan’s Pomade. Її першими продавцями були модні "
    "чоловічі перукарні лондонського Вест-Енду; зростання попиту перенесло виробництво до "
    "невеликої фабрики в Хайґейті, а згодом — у більші приміщення.\n\n"
    "Попри труднощі з сировиною під час обох світових воєн, Morgan’s продовжував розвивати "
    "експорт: Індія стала найбільшим ринком у воєнний період, а після війни важливими стали "
    "Західна Африка й Нігерія. У 1935 році бренд придбав фабрику в Hornsey Rise, а пізніше "
    "побудував спеціалізоване виробництво у Вітстейблі, графство Кент. Від 1970-х асортимент "
    "розширився шампунями й стайлінгом, приватними лінійками для барберів і перукарів, "
    "професійним доглядом та Men’s Retro Barber range. Сьогодні Morgan’s продається більш ніж "
    "на 50 експортних ринках."
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
        .where(brands.c.slug == "morgan-s-pomade")
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
        .where(brands.c.slug == "morgan-s-pomade")
        .values(
            description_uk="Незалежний британський виробник професійних засобів для волосся, бороди, гоління, ароматів і догляду за шкірою.",
            description_en="An independent British maker of professional hair, beard, shaving, fragrance and skin-care products.",
            history_uk="Morgan's засновано в Лондоні 1873 року; після першого шампуню за кілька років з'явилася Morgan's Pomade.",
            history_en="Morgan's was established in London in 1873; Morgan's Pomade followed its first shampoo a few years later.",
        )
    )
