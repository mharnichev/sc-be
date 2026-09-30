"""Update Floid profile from its official story page.

Revision ID: 0077_floid_official_copy
Revises: 0076_american_crew_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0077_floid_official_copy"
down_revision = "0076_american_crew_copy"
branch_labels = None
depends_on = None


DESCRIPTION_EN = (
    "The symbol of aftershave and proper shaving. Since 1932\n\n"
    "A complete shaving and body care system that is the perfect marriage of past and future, "
    "classicism and innovation. Specialised products with advanced formulations for a flawless shave "
    "and daily body care. Dedicated to those who want to give themselves extra attention."
)
DESCRIPTION_UK = (
    "Символ aftershave і правильного гоління з 1932 року.\n\n"
    "Повна система для гоління та догляду за тілом, що є ідеальним поєднанням минулого й майбутнього, "
    "класики та інновацій. Спеціалізовані продукти з передовими формулами для бездоганного гоління та "
    "щоденного догляду за тілом. Для тих, хто хоче приділяти собі більше уваги."
)
HISTORY_EN = (
    "90 years ago, J.B. Cendrós, the owner of the \"Buenos Aires\" barbershop in Barcelona, sensed "
    "the lack of a product to complete the shaving ritual. He created an alcohol-based, invigorating "
    "and soothing formulation, rich in beneficial ingredients for the skin: the secret of Floïd, what "
    "would make it an iconic aftershave lotion with an inimitable fragrance for almost a century."
)
HISTORY_UK = (
    "90 років тому J. B. Cendrós, власник барбершопу «Buenos Aires» у Барселоні, відчув, що для "
    "завершення ритуалу гоління бракує одного продукту. Він створив спиртову, тонізувальну й "
    "заспокійливу формулу, багату на корисні для шкіри інгредієнти: секрет Floïd, що майже на "
    "століття зробив його культовим лосьйоном після гоління з неповторним ароматом."
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
        .where(brands.c.slug == "floid")
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
        .where(brands.c.slug == "floid")
        .values(
            description_uk="Іспанський бренд професійного гоління та aftershave, відомий тонізувальними лосьйонами після гоління.",
            description_en="A Spanish professional shaving and aftershave brand known for toning post-shave lotions.",
            history_uk="У 1932 році J. B. Cendrós створив у Барселоні формулу, що стала основою культового aftershave Floïd.",
            history_en="In 1932, J. B. Cendrós created in Barcelona the formula that became Floïd's iconic aftershave.",
        )
    )
