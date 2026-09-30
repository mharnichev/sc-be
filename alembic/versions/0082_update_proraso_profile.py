"""Update Proraso profile from its official about page.

Revision ID: 0082_proraso_official_copy
Revises: 0081_kent_official_copy
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0082_proraso_official_copy"
down_revision = "0081_kent_official_copy"
branch_labels = None
depends_on = None

DESCRIPTION_EN = "A symbol of Italianness which has been conquering the world for 70 years.\n\nProraso is not only a great brand, since 1948 it has been growing together with Italians, and its excellence has changed the habit of beard shaving.\n\nProraso today is also a complete line for beard and moustache care."
DESCRIPTION_UK = "Символ італійськості, який понад 70 років підкорює світ.\n\nProraso — не просто великий бренд: з 1948 року він розвивався разом з італійцями, а його досконалість змінила звичку голити бороду.\n\nСьогодні Proraso — це також повна лінійка для догляду за бородою та вусами."
HISTORY_EN = "In the company founded in 1908 by his father Ludovico, Piero Martelli invented the Pre-Shave Cream in 1948: efficient and modern, it was first adopted by barbers, who immediately appreciated its professionalism, then by an ever growing number of Italians. It became an icon, witness to the principle inspiring the company and its mission: the choice of proposing professional quality products for a pleasant and impeccable shave, at the barber’s or at home.\n\nOver the years, Proraso’s laboratories have given birth to timeless classics which embrace the present and look to the future: a complete shaving system, made of highly specialised items, professional service, highly developed formulas and natural ingredients.\n\nToday, the company is run by the fourth generation, and its mission is always the same: to make emblematic and highly specialised products for beard shaving and care. The perfect match between past and future, between the classic tradition and innovation, but also communication, protection of an art, of its techniques and of the people who practise it."
HISTORY_UK = "У компанії, заснованій 1908 року Людовіко Мартеллі, його син П’єро Мартеллі у 1948 році винайшов крем Pre-Shave: ефективний і сучасний, спочатку його обрали барбери, які відразу оцінили професійну якість, а згодом дедалі більше італійців. Він став іконою та втіленням місії компанії — пропонувати продукти професійної якості для приємного й бездоганного гоління у барбершопі та вдома.\n\nЗ роками в лабораторіях Proraso з’являлися класичні засоби, що поєднують сучасність і майбутнє: повна система гоління з високоспеціалізованими продуктами, професійною якістю, розвиненими формулами й натуральними інгредієнтами.\n\nСьогодні компанією керує четверте покоління родини, а її місія залишається незмінною: створювати знакові, вузькоспеціалізовані засоби для гоління й догляду за бородою. Це поєднання минулого й майбутнього, класичної традиції та інновацій, а також підтримка мистецтва барберингу, його технік і людей, які ним займаються."


def upgrade() -> None:
    brands = sa.table("brands", sa.column("slug", sa.String()), sa.column("description_uk", sa.Text()), sa.column("description_en", sa.Text()), sa.column("history_uk", sa.Text()), sa.column("history_en", sa.Text()))
    op.get_bind().execute(brands.update().where(brands.c.slug == "proraso").values(description_uk=DESCRIPTION_UK, description_en=DESCRIPTION_EN, history_uk=HISTORY_UK, history_en=HISTORY_EN))


def downgrade() -> None:
    brands = sa.table("brands", sa.column("slug", sa.String()), sa.column("description_uk", sa.Text()), sa.column("description_en", sa.Text()), sa.column("history_uk", sa.Text()), sa.column("history_en", sa.Text()))
    op.get_bind().execute(brands.update().where(brands.c.slug == "proraso").values(description_uk="Італійський бренд засобів для традиційного гоління та догляду за бородою з професійною барберською спадщиною.", description_en="An Italian traditional shaving and beard-care brand rooted in professional barbering heritage.", history_uk="Proraso з'явився 1948 року в лабораторіях Ludovico Martelli — флорентійської компанії, заснованої в 1908 році.", history_en="Proraso was born in 1948 in the laboratories of Ludovico Martelli, a Florence company founded in 1908."))
