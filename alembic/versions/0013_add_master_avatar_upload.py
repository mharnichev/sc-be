"""add master avatar upload relation

Revision ID: 0013_master_avatar_upload
Revises: 0012_master_photo_upload
Create Date: 2026-05-13 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_master_avatar_upload"
down_revision = "0012_master_photo_upload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("masters", sa.Column("avatar_url", sa.String(length=500), nullable=True))
    op.add_column("masters", sa.Column("avatar_upload_id", sa.Integer(), nullable=True))
    op.create_index("ix_masters_avatar_upload_id", "masters", ["avatar_upload_id"])
    op.create_foreign_key(
        "fk_masters_avatar_upload_id_uploads",
        "masters",
        "uploads",
        ["avatar_upload_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_masters_avatar_upload_id_uploads", "masters", type_="foreignkey")
    op.drop_index("ix_masters_avatar_upload_id", table_name="masters")
    op.drop_column("masters", "avatar_upload_id")
    op.drop_column("masters", "avatar_url")
