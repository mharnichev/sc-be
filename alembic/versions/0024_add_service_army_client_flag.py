"""add service army client flag

Revision ID: 0024_service_army_client_flag
Revises: 0023_master_telegram_chat_id
Create Date: 2026-06-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0024_service_army_client_flag"
down_revision = "0023_master_telegram_chat_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("base_services", "barber_services"):
        op.add_column(
            table_name,
            sa.Column("is_army_client", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
        op.alter_column(table_name, "is_army_client", server_default=None)


def downgrade() -> None:
    for table_name in ("barber_services", "base_services"):
        op.drop_column(table_name, "is_army_client")
