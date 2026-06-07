"""add telegram contacts

Revision ID: 0025_telegram_contacts
Revises: 0024_service_army_client_flag
Create Date: 2026-06-07 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025_telegram_contacts"
down_revision = "0024_service_army_client_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=128), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("language_code", sa.String(length=16), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("linked_customer_id", sa.Integer(), nullable=True),
        sa.Column("last_update_id", sa.Integer(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_update", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["linked_customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_telegram_contacts_chat_id"), "telegram_contacts", ["chat_id"], unique=True)
    op.create_index(op.f("ix_telegram_contacts_telegram_user_id"), "telegram_contacts", ["telegram_user_id"])
    op.create_index(op.f("ix_telegram_contacts_username"), "telegram_contacts", ["username"])
    op.create_index(op.f("ix_telegram_contacts_phone"), "telegram_contacts", ["phone"])
    op.create_index(op.f("ix_telegram_contacts_linked_customer_id"), "telegram_contacts", ["linked_customer_id"])
    op.create_index(op.f("ix_telegram_contacts_last_seen_at"), "telegram_contacts", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_contacts_last_seen_at"), table_name="telegram_contacts")
    op.drop_index(op.f("ix_telegram_contacts_linked_customer_id"), table_name="telegram_contacts")
    op.drop_index(op.f("ix_telegram_contacts_phone"), table_name="telegram_contacts")
    op.drop_index(op.f("ix_telegram_contacts_username"), table_name="telegram_contacts")
    op.drop_index(op.f("ix_telegram_contacts_telegram_user_id"), table_name="telegram_contacts")
    op.drop_index(op.f("ix_telegram_contacts_chat_id"), table_name="telegram_contacts")
    op.drop_table("telegram_contacts")
