"""Update Standard Issue profile copy.

Revision ID: 0075_standard_issue_copy
Revises: 0075_brand_website
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0075_standard_issue_copy"
down_revision = "0075_brand_website"
branch_labels = None
depends_on = None


DESCRIPTION_EN = (
    "Standard Issue offers a range of everyday staples built for barbershops and salons. "
    "Our focus is simple: to deliver the essential tools and consumables professionals rely on — "
    "reliable, accessible, and fairly priced. From combs and brushes to razors, neck strips, "
    "towels, and gloves, we make sure the basics are covered so barbers can focus on their craft.\n\n"
    "We created Standard Issue to take the hassle out of sourcing shop essentials. Instead of "
    "juggling multiple suppliers, you’ll find the core products you need in one place — consistent "
    "in quality, designed with input from working barbers, and made to perform every day without "
    "unnecessary cost or complication."
)
DESCRIPTION_UK = (
    "Standard Issue пропонує асортимент щоденних базових товарів для барбершопів і салонів. "
    "Наша мета проста: постачати необхідні інструменти й витратні матеріали, на які покладаються "
    "професіонали — надійні, доступні та за чесною ціною. Від гребінців і щіток до бритв, "
    "комірцевих стрічок, рушників і рукавичок — ми дбаємо, щоб усе необхідне було під рукою, "
    "а барбери могли зосередитися на своїй майстерності.\n\n"
    "Ми створили Standard Issue, щоб позбавити заклади зайвого клопоту з пошуком необхідних товарів. "
    "Замість роботи з багатьма постачальниками, тут можна знайти основні потрібні продукти в одному "
    "місці — стабільної якості, розроблені з урахуванням досвіду практикуючих барберів і створені "
    "для щоденної роботи без зайвих витрат чи ускладнень."
)
HISTORY_EN = (
    "Standard Issue is about practicality; our products aren’t built to make a statement, they’re "
    "built to get the job done. That’s why our range continues to expand with feedback from the "
    "industry — if something’s missing, we listen and create it.\n\n"
    "Standard Issue. Tools that work as hard as you do."
)
HISTORY_UK = (
    "Standard Issue — це практичність: наші товари створені не для гучних заяв, а щоб виконувати "
    "свою роботу. Саме тому асортимент продовжує розширюватися завдяки відгукам індустрії — якщо "
    "чогось бракує, ми слухаємо та створюємо це.\n\n"
    "Standard Issue. Інструменти, що працюють так само наполегливо, як і ви."
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
        .where(brands.c.slug == "standard-issue")
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
        .where(brands.c.slug == "standard-issue")
        .values(
            description_uk="Бренд професійних барберських товарів і аксесуарів для робочого місця та домашнього догляду.",
            description_en="A brand of professional barber supplies and accessories for the workstation and home grooming.",
            history_uk="Standard Issue фокусується на практичних інструментах і витратних матеріалах для барберингу.",
            history_en="Standard Issue focuses on practical barbering tools and consumables.",
        )
    )
