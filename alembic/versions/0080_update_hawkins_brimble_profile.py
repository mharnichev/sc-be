"""Update Hawkins & Brimble profile from its official story page.

Revision ID: 0080_hawkins_official_copy
Revises: 0079_dapper_dan_official
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0080_hawkins_official_copy"
down_revision = "0079_dapper_dan_official"
branch_labels = None
depends_on = None


DESCRIPTION_EN = (
    "At Hawkins & Brimble, we’ve always had an appreciation of the finer things in life. So, when "
    "Stephen, our Founder, was struggling to find male grooming products that were just as gentle on "
    "his sensitive skin as they were on the planet – we decided to helps gents around the world raise "
    "the bar.\n\nBorn in London. Worn by gentlemen."
)
DESCRIPTION_UK = (
    "У Hawkins & Brimble ми завжди цінували найкраще в житті. Тож коли наш засновник Стівен не міг "
    "знайти засоби чоловічого догляду, які були б такими ж делікатними до його чутливої шкіри, як і "
    "до планети, ми вирішили допомогти джентльменам у всьому світі підняти планку.\n\n"
    "Народжений у Лондоні. Створений для джентльменів."
)
HISTORY_EN = (
    "Inspired by the superior grooming standards forged behind the oak panelled doors of Mayfair’s "
    "exclusive 17th century members’ clubs, we’re helping the next generation of gentlemen make their "
    "mark. From the finest natural ingredients to our iconic signature scent – every detail of our "
    "top-to-toe range has been carefully crafted under the watchful gaze of a master barber to ensure "
    "only the very best will do.\n\n"
    "We really care about what’s going into our products. That’s why you won’t find anything but the "
    "gentlest natural ingredients listed on our labels. No parabens. No SLS. No added nonsense.\n\n"
    "We’re working on our legacy, right now. By committing to more sustainable reusable packaging and "
    "natural ingredients – we’re leading the charge to make superior grooming standards, the industry "
    "standard.\n\n"
    "We’re working hard to demystify the world of male grooming – educating gents around the world on "
    "the transformational power of superior grooming standards, natural ingredients and positive choices."
)
HISTORY_UK = (
    "Натхненні високими стандартами догляду, сформованими за дубовими дверима ексклюзивних лондонських "
    "клубів Мейфера XVII століття, ми допомагаємо наступному поколінню джентльменів заявити про себе. "
    "Від найкращих натуральних інгредієнтів до нашого знакового фірмового аромату — кожна деталь "
    "нашого асортименту від голови до п’ят ретельно створена під пильним наглядом майстра-барбера, "
    "щоб найкраще було єдиним прийнятним варіантом.\n\n"
    "Ми справді дбаємо про те, що входить до складу наших продуктів. Тому на етикетках ви знайдете "
    "лише найніжніші натуральні інгредієнти. Без парабенів. Без SLS. Без зайвого.\n\n"
    "Ми створюємо свою спадщину вже зараз: завдяки більш екологічній багаторазовій упаковці й "
    "натуральним інгредієнтам ми прагнемо зробити найвищі стандарти грумінгу стандартом індустрії.\n\n"
    "Ми також працюємо над тим, щоб зробити світ чоловічого догляду зрозумілішим, навчаючи джентльменів "
    "у всьому світі перетворювальній силі високих стандартів грумінгу, натуральних інгредієнтів і "
    "свідомого вибору."
)


def upgrade() -> None:
    brands = sa.table("brands", sa.column("slug", sa.String()), sa.column("description_uk", sa.Text()), sa.column("description_en", sa.Text()), sa.column("history_uk", sa.Text()), sa.column("history_en", sa.Text()))
    op.get_bind().execute(brands.update().where(brands.c.slug == "hawkins-brimble").values(description_uk=DESCRIPTION_UK, description_en=DESCRIPTION_EN, history_uk=HISTORY_UK, history_en=HISTORY_EN))


def downgrade() -> None:
    brands = sa.table("brands", sa.column("slug", sa.String()), sa.column("description_uk", sa.Text()), sa.column("description_en", sa.Text()), sa.column("history_uk", sa.Text()), sa.column("history_en", sa.Text()))
    op.get_bind().execute(brands.update().where(brands.c.slug == "hawkins-brimble").values(description_uk="Бренд чоловічого догляду для волосся, обличчя, гоління та бороди, орієнтований на прості щоденні ритуали.", description_en="A men's-care brand for hair, face, shaving and beard routines, designed for straightforward daily rituals.", history_uk="Hawkins & Brimble створює сучасні засоби для щоденного чоловічого догляду та охайного вигляду.", history_en="Hawkins & Brimble creates contemporary products for everyday men's care and a well-groomed look."))
