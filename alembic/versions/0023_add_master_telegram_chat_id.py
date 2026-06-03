"""add master telegram chat id

Revision ID: 0023_master_telegram_chat_id
Revises: 0022_master_booking_redirect
Create Date: 2026-06-02 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023_master_telegram_chat_id"
down_revision = "0022_master_booking_redirect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("masters", sa.Column("telegram_chat_id", sa.String(length=128), nullable=True))
    op.create_index(op.f("ix_masters_telegram_chat_id"), "masters", ["telegram_chat_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_masters_telegram_chat_id"), table_name="masters")
    op.drop_column("masters", "telegram_chat_id")
