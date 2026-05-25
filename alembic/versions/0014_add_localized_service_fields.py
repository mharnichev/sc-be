"""add localized service fields

Revision ID: 0014_localized_service_fields
Revises: 0013_master_avatar_upload
Create Date: 2026-05-25 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_localized_service_fields"
down_revision = "0013_master_avatar_upload"
branch_labels = None
depends_on = None


DEFAULT_TITLE_EN = {
    "Традиційне гоління": "Traditional shave",
    "Дитяча стрижка": "Kids haircut",
    "Стрижка машинкою+стрижка бороди": "Clipper cut + beard trim",
    "Стрижка+борода": "Haircut + beard trim",
    "Стрижка бороди": "Beard trim",
    "Стрижка машинкою": "Clipper cut",
    "Стрижка": "Haircut",
}

DEFAULT_DESCRIPTION_EN = {
    "Традиційне гоління": "Classic straight-razor shave with hot towel preparation and finishing care.",
    "Дитяча стрижка": "Haircut for children with a clean, comfortable finish.",
    "Стрижка машинкою+стрижка бороди": "Clipper haircut paired with beard shaping and contour cleanup.",
    "Стрижка+борода": "Complete haircut with beard shaping, edging, and styling.",
    "Стрижка бороди": "Beard shaping, length adjustment, and clean contouring.",
    "Стрижка машинкою": "Even clipper haircut with clean edges and neckline detail.",
    "Стрижка": "Classic haircut with shape, texture, and styling.",
}


def upgrade() -> None:
    for table_name in ("base_services", "barber_services"):
        op.add_column(table_name, sa.Column("title_uk", sa.String(length=255), nullable=True))
        op.add_column(table_name, sa.Column("title_en", sa.String(length=255), nullable=True))
        op.add_column(table_name, sa.Column("description_uk", sa.Text(), nullable=True))
        op.add_column(table_name, sa.Column("description_en", sa.Text(), nullable=True))
        op.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET title_uk = name,
                    description_uk = description
                WHERE title_uk IS NULL
                    AND name IS NOT NULL
                """
            )
        )
        for title_uk, title_en in DEFAULT_TITLE_EN.items():
            op.execute(
                sa.text(
                    f"""
                    UPDATE {table_name}
                    SET title_en = :title_en
                    WHERE title_en IS NULL
                        AND name = :title_uk
                    """
                ).bindparams(title_uk=title_uk, title_en=title_en)
            )
        for title_uk, description_en in DEFAULT_DESCRIPTION_EN.items():
            op.execute(
                sa.text(
                    f"""
                    UPDATE {table_name}
                    SET description_en = :description_en
                    WHERE description_en IS NULL
                        AND name = :title_uk
                    """
                ).bindparams(title_uk=title_uk, description_en=description_en)
            )


def downgrade() -> None:
    for table_name in ("barber_services", "base_services"):
        op.drop_column(table_name, "description_en")
        op.drop_column(table_name, "description_uk")
        op.drop_column(table_name, "title_en")
        op.drop_column(table_name, "title_uk")
