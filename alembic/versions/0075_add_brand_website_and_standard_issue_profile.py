"""Add brand websites and refresh the Standard Issue profile.

Revision ID: 0075_brand_website
Revises: 0074_brand_localized_profiles
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0075_brand_website"
down_revision = "0074_brand_localized_profiles"
branch_labels = None
depends_on = None


STANDARD_ISSUE_DESCRIPTION_UK = (
    "Standard Issue Barber Supplies створює якісні базові товари для барбершопів і салонів: "
    "надійні та функціональні інструменти, витратні матеріали, гребінці й щітки для щоденної професійної роботи."
)
STANDARD_ISSUE_DESCRIPTION_EN = (
    "Standard Issue Barber Supplies makes quality staples for barbershops and salons: "
    "reliable, functional tools, consumables, combs and brushes for daily professional use."
)
STANDARD_ISSUE_HISTORY_UK = (
    "Асортимент бренду побудований навколо основної лінійки, витратних матеріалів, гребінців і щіток — "
    "усього необхідного для зручної та стабільної роботи барбера."
)
STANDARD_ISSUE_HISTORY_EN = (
    "The range centres on core essentials, consumables, combs and brushes — "
    "everything needed for straightforward, dependable barbering work."
)


def upgrade() -> None:
    op.add_column("brands", sa.Column("website", sa.String(length=500), nullable=True))

    brands = sa.table(
        "brands",
        sa.column("slug", sa.String()),
        sa.column("website", sa.String()),
        sa.column("description_uk", sa.Text()),
        sa.column("description_en", sa.Text()),
        sa.column("history_uk", sa.Text()),
        sa.column("history_en", sa.Text()),
    )
    op.execute(
        brands.update()
        .where(brands.c.slug == "standard-issue")
        .values(
            website="https://standardissue.supply/",
            description_uk=STANDARD_ISSUE_DESCRIPTION_UK,
            description_en=STANDARD_ISSUE_DESCRIPTION_EN,
            history_uk=STANDARD_ISSUE_HISTORY_UK,
            history_en=STANDARD_ISSUE_HISTORY_EN,
        )
    )


def downgrade() -> None:
    op.drop_column("brands", "website")
