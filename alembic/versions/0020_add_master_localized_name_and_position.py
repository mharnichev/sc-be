"""add master localized name and position

Revision ID: 0020_master_localized
Revises: 0019_messaging_campaigns
Create Date: 2026-05-30 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0020_master_localized"
down_revision = "0019_messaging_campaigns"
branch_labels = None
depends_on = None


master_position = postgresql.ENUM(
    "ambassador",
    "senior_master",
    "master",
    name="masterposition",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    master_position.create(bind, checkfirst=True)

    op.add_column("masters", sa.Column("last_name", sa.String(length=255), nullable=True))
    op.add_column("masters", sa.Column("first_name_en", sa.String(length=255), nullable=True))
    op.add_column("masters", sa.Column("last_name_en", sa.String(length=255), nullable=True))
    op.add_column(
        "masters",
        sa.Column(
            "position",
            sa.Enum("ambassador", "senior_master", "master", name="masterposition"),
            server_default="master",
            nullable=False,
        ),
    )
    op.alter_column("masters", "position", server_default=None)


def downgrade() -> None:
    op.drop_column("masters", "position")
    op.drop_column("masters", "last_name_en")
    op.drop_column("masters", "first_name_en")
    op.drop_column("masters", "last_name")

    bind = op.get_bind()
    master_position.drop(bind, checkfirst=True)
