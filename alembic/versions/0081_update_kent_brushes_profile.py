"""Update Kent Brushes profile from its official about page.

Revision ID: 0081_kent_official_copy
Revises: 0080_hawkins_official_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0081_kent_official_copy"
down_revision = "0080_hawkins_official_copy"
branch_labels = None
depends_on = None


DESCRIPTION_EN = (
    "Kent Brushes is the oldest brush manufacturer in the world.\n\n"
    "Proudly creating beautiful brushes since 1777, Kent Brushes is a British, independent, family-run "
    "business that has been chaired by just two families throughout almost 250 years of trading.\n\n"
    "Kent Brushes was established in the reign of George III, in 1777. We have upheld our commitment to "
    "producing quality hair, shaving and personal care brushes for almost 250 years and are renowned for "
    "being one of the finest hairbrush manufacturers in the world. We are one of the oldest independent "
    "British companies trading today and have held our Royal Warrant for hairbrushes for nine consecutive "
    "Sovereign reigns."
)
DESCRIPTION_UK = (
    "Kent Brushes — найстаріший у світі виробник щіток.\n\n"
    "З 1777 року Kent Brushes створює прекрасні щітки. Це незалежний британський сімейний бізнес, яким "
    "протягом майже 250 років керували лише дві родини.\n\n"
    "Kent Brushes було засновано за правління Георга III у 1777 році. Майже 250 років бренд зберігає "
    "відданість виробництву якісних щіток для волосся, гоління та особистого догляду. Це одна з "
    "найстаріших незалежних британських компаній, що працюють донині, і власник Королівського ордера "
    "на щітки протягом дев’яти поспіль правлінь монархів."
)
HISTORY_EN = (
    "Kent Brushes was heavily involved in both World Wars, equipping millions of brushes for troops in "
    "the Army, Navy, and RAF. From hair, tooth, shaving, cloth, shoe and button brushes to custom brushes "
    "used for cleaning the fuselage of aeroplanes and the barrels of anti-tank guns. We were even elected "
    "to create a top-secret brush in which maps and compasses were concealed to help the war effort."
)
HISTORY_UK = (
    "Kent Brushes активно працювала в обох світових війнах, забезпечуючи мільйонами щіток війська "
    "армії, флоту та Королівських ВПС. Від щіток для волосся, зубів, гоління, одягу, взуття й ґудзиків "
    "до спеціальних щіток для очищення фюзеляжів літаків і стволів протитанкових гармат. Компанію навіть "
    "обрали для створення надсекретної щітки, у якій приховували карти та компаси для підтримки воєнних зусиль."
)


def upgrade() -> None:
    brands = sa.table("brands", sa.column("slug", sa.String()), sa.column("description_uk", sa.Text()), sa.column("description_en", sa.Text()), sa.column("history_uk", sa.Text()), sa.column("history_en", sa.Text()))
    op.get_bind().execute(brands.update().where(brands.c.slug == "kent-brushes").values(description_uk=DESCRIPTION_UK, description_en=DESCRIPTION_EN, history_uk=HISTORY_UK, history_en=HISTORY_EN))


def downgrade() -> None:
    brands = sa.table("brands", sa.column("slug", sa.String()), sa.column("description_uk", sa.Text()), sa.column("description_en", sa.Text()), sa.column("history_uk", sa.Text()), sa.column("history_en", sa.Text()))
    op.get_bind().execute(brands.update().where(brands.c.slug == "kent-brushes").values(description_uk="Незалежний британський виробник щіток і гребінців для волосся, бороди, гоління та щоденного догляду.", description_en="An independent British maker of brushes and combs for hair, beard, shaving and everyday grooming.", history_uk="Компанію G. B. Kent заснував Вільям Кент у Лондоні 1777 року; бренд має майже 250-річну історію виробництва щіток.", history_en="William Kent founded G. B. Kent in London in 1777; the brand has an almost 250-year history of brush making."))
