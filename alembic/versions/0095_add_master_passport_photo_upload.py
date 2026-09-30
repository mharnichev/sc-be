"""add master passport photo upload relation

Revision ID: 0095_master_passport_photo
Revises: 0094_remove_workplace_keep_combs
Create Date: 2026-09-25 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0095_master_passport_photo"
down_revision = "0094_workplace_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("masters", sa.Column("passport_photo_url", sa.String(length=500), nullable=True))
    op.add_column("masters", sa.Column("passport_photo_upload_id", sa.Integer(), nullable=True))
    op.create_index("ix_masters_passport_photo_upload_id", "masters", ["passport_photo_upload_id"])
    op.create_foreign_key(
        "fk_masters_passport_photo_upload_id_uploads",
        "masters",
        "uploads",
        ["passport_photo_upload_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_masters_passport_photo_upload_id_uploads", "masters", type_="foreignkey")
    op.drop_index("ix_masters_passport_photo_upload_id", table_name="masters")
    op.drop_column("masters", "passport_photo_upload_id")
    op.drop_column("masters", "passport_photo_url")
