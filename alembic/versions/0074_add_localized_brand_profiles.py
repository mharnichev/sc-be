"""Add localized brand profiles.

Revision ID: 0074_brand_localized_profiles
Revises: 0073_product_name_parts
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0074_brand_localized_profiles"
down_revision = "0073_product_name_parts"
branch_labels = None
depends_on = None


PROFILES = {
    "american-crew": (
        "Професійний американський бренд чоловічого грумінгу для волосся, гоління, бороди й догляду за тілом.",
        "A professional American men's-grooming brand for hair, shaving, beard and body care.",
        "American Crew заснував у 1994 році стиліст Девід Раккулья для чоловіків і барберів.",
        "American Crew was founded in 1994 by stylist David Raccuglia for men and barbers.",
    ),
    "depot": (
        "Італійський професійний бренд чоловічого догляду для барбершопу та дому: волосся, гоління, борода, тіло й аромати.",
        "An Italian professional male-grooming brand for the barbershop and home: hair, shaving, beard, body care and fragrances.",
        "DEPOT розвиває сучасну культуру барберингу й поєднує професійний сервіс із домашнім ритуалом догляду.",
        "DEPOT develops contemporary barbering culture and connects professional service with home-care rituals.",
    ),
    "dapper-dan": (
        "Британський бренд засобів для укладання та чоловічого грумінгу: помади, пасти, тоніки й засоби для гоління.",
        "A British styling and men's-grooming brand offering pomades, pastes, tonics and shaving care.",
        "Dapper Dan поєднує засоби для укладання з барберською культурою класичних і сучасних зачісок.",
        "Dapper Dan combines styling products with barbering culture for classic and contemporary looks.",
    ),
    "floid": (
        "Іспанський бренд професійного гоління та aftershave, відомий тонізувальними лосьйонами після гоління.",
        "A Spanish professional shaving and aftershave brand known for toning post-shave lotions.",
        "У 1932 році J. B. Cendrós створив у Барселоні формулу, що стала основою культового aftershave Floïd.",
        "In 1932, J. B. Cendrós created in Barcelona the formula that became Floïd's iconic aftershave.",
    ),
    "hawkins-brimble": (
        "Бренд чоловічого догляду для волосся, обличчя, гоління та бороди, орієнтований на прості щоденні ритуали.",
        "A men's-care brand for hair, face, shaving and beard routines, designed for straightforward daily rituals.",
        "Hawkins & Brimble створює сучасні засоби для щоденного чоловічого догляду та охайного вигляду.",
        "Hawkins & Brimble creates contemporary products for everyday men's care and a well-groomed look.",
    ),
    "kent-brushes": (
        "Незалежний британський виробник щіток і гребінців для волосся, бороди, гоління та щоденного догляду.",
        "An independent British maker of brushes and combs for hair, beard, shaving and everyday grooming.",
        "Компанію G. B. Kent заснував Вільям Кент у Лондоні 1777 року; бренд має майже 250-річну історію виробництва щіток.",
        "William Kent founded G. B. Kent in London in 1777; the brand has an almost 250-year history of brush making.",
    ),
    "marvis": (
        "Італійський бренд зубної пасти та догляду за ротовою порожниною з виразними смаками, ароматами й дизайном.",
        "An Italian toothpaste and oral-care brand with distinctive flavours, aromas and design.",
        "Marvis переосмислює зубну пасту як щоденний сенсорний ритуал і задоволення.",
        "Marvis reimagines toothpaste as a daily sensory ritual and pleasure.",
    ),
    "morgan-s-pomade": (
        "Незалежний британський виробник професійних засобів для волосся, бороди, гоління, ароматів і догляду за шкірою.",
        "An independent British maker of professional hair, beard, shaving, fragrance and skin-care products.",
        "Morgan's засновано в Лондоні 1873 року; після першого шампуню за кілька років з'явилася Morgan's Pomade.",
        "Morgan's was established in London in 1873; Morgan's Pomade followed its first shampoo a few years later.",
    ),
    "proraso": (
        "Італійський бренд засобів для традиційного гоління та догляду за бородою з професійною барберською спадщиною.",
        "An Italian traditional shaving and beard-care brand rooted in professional barbering heritage.",
        "Proraso з'явився 1948 року в лабораторіях Ludovico Martelli — флорентійської компанії, заснованої в 1908 році.",
        "Proraso was born in 1948 in the laboratories of Ludovico Martelli, a Florence company founded in 1908.",
    ),
    "reuzel": (
        "Барберський бренд помад, засобів для укладання, догляду за волоссям і бородою, натхнений класичною культурою барбершопу.",
        "A barbering brand of pomades, styling, hair-care and beard-care products inspired by classic barbershop culture.",
        "Reuzel розвиває виразну барберську айдентику та продукти для індивідуального стилю.",
        "Reuzel develops a distinctive barbering identity and products for individual style.",
    ),
    "slick-gorilla": (
        "Сучасний бренд чоловічого грумінгу, зосереджений на засобах для укладання та повсякденного догляду.",
        "A contemporary men's-grooming brand focused on styling and everyday care.",
        "Slick Gorilla створює прості інструменти для самовираження через зачіску.",
        "Slick Gorilla creates simple tools for self-expression through hair.",
    ),
    "standard-issue": (
        "Бренд професійних барберських товарів і аксесуарів для робочого місця та домашнього догляду.",
        "A brand of professional barber supplies and accessories for the workstation and home grooming.",
        "Standard Issue фокусується на практичних інструментах і витратних матеріалах для барберингу.",
        "Standard Issue focuses on practical barbering tools and consumables.",
    ),
    "the-bluebeards-revenge": (
        "Британський бренд чоловічого догляду для гоління, волосся, обличчя та тіла з виразною барберською айдентикою.",
        "A British men's-care brand for shaving, hair, face and body with a distinctive barbering identity.",
        "The BlueBeards Revenge поєднує професійні засоби з грайливою барберською естетикою.",
        "The BlueBeards Revenge combines professional products with a playful barbering aesthetic.",
    ),
    "uppercut-deluxe": (
        "Бренд чоловічого грумінгу для укладання, догляду за волоссям, гоління та бороди, натхнений класичною барберською культурою.",
        "A men's-grooming brand for styling, hair care, shaving and beard care, inspired by classic barbering culture.",
        "Uppercut Deluxe розвиває функціональні засоби для щоденних укладок у традиції барбершопу.",
        "Uppercut Deluxe develops functional products for everyday styling in the barbershop tradition.",
    ),
}


def upgrade() -> None:
    op.add_column("brands", sa.Column("description_uk", sa.Text(), nullable=True))
    op.add_column("brands", sa.Column("description_en", sa.Text(), nullable=True))
    op.add_column("brands", sa.Column("history_uk", sa.Text(), nullable=True))
    op.add_column("brands", sa.Column("history_en", sa.Text(), nullable=True))

    brands = sa.table(
        "brands",
        sa.column("slug", sa.String()),
        sa.column("description_uk", sa.Text()),
        sa.column("description_en", sa.Text()),
        sa.column("history_uk", sa.Text()),
        sa.column("history_en", sa.Text()),
    )
    bind = op.get_bind()
    for slug, (description_uk, description_en, history_uk, history_en) in PROFILES.items():
        bind.execute(
            brands.update()
            .where(brands.c.slug == slug)
            .values(
                description_uk=description_uk,
                description_en=description_en,
                history_uk=history_uk,
                history_en=history_en,
            )
        )


def downgrade() -> None:
    op.drop_column("brands", "history_en")
    op.drop_column("brands", "history_uk")
    op.drop_column("brands", "description_en")
    op.drop_column("brands", "description_uk")
