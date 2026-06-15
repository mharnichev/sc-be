"""add master availability windows

Revision ID: 0027_master_availability_windows
Revises: 0026_blog_subscriptions
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_master_availability_windows"
down_revision = "0026_blog_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "master_availability_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_master_availability_windows_master_id"),
        "master_availability_windows",
        ["master_id"],
    )
    op.create_index(
        op.f("ix_master_availability_windows_start_at"),
        "master_availability_windows",
        ["start_at"],
    )
    op.create_index(
        op.f("ix_master_availability_windows_end_at"),
        "master_availability_windows",
        ["end_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_master_availability_windows_end_at"), table_name="master_availability_windows")
    op.drop_index(op.f("ix_master_availability_windows_start_at"), table_name="master_availability_windows")
    op.drop_index(op.f("ix_master_availability_windows_master_id"), table_name="master_availability_windows")
    op.drop_table("master_availability_windows")
