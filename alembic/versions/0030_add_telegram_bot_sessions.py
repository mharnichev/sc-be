"""add telegram bot sessions

Revision ID: 0030_telegram_bot_sessions
Revises: 0029_two_hour_sms_reminder
Create Date: 2026-06-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0030_telegram_bot_sessions"
down_revision = "0029_two_hour_sms_reminder"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_bot_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("telegram_contact_id", sa.Integer(), nullable=True),
        sa.Column("linked_customer_id", sa.Integer(), nullable=True),
        sa.Column("selected_master_id", sa.Integer(), nullable=True),
        sa.Column("selected_service_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["linked_customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["selected_master_id"], ["masters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["selected_service_id"], ["barber_services.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["telegram_contact_id"], ["telegram_contacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_telegram_bot_sessions_chat_id"), "telegram_bot_sessions", ["chat_id"], unique=True)
    op.create_index(op.f("ix_telegram_bot_sessions_expires_at"), "telegram_bot_sessions", ["expires_at"])
    op.create_index(op.f("ix_telegram_bot_sessions_last_seen_at"), "telegram_bot_sessions", ["last_seen_at"])
    op.create_index(op.f("ix_telegram_bot_sessions_linked_customer_id"), "telegram_bot_sessions", ["linked_customer_id"])
    op.create_index(op.f("ix_telegram_bot_sessions_selected_master_id"), "telegram_bot_sessions", ["selected_master_id"])
    op.create_index(op.f("ix_telegram_bot_sessions_selected_service_id"), "telegram_bot_sessions", ["selected_service_id"])
    op.create_index(op.f("ix_telegram_bot_sessions_state"), "telegram_bot_sessions", ["state"])
    op.create_index(op.f("ix_telegram_bot_sessions_telegram_contact_id"), "telegram_bot_sessions", ["telegram_contact_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_bot_sessions_telegram_contact_id"), table_name="telegram_bot_sessions")
    op.drop_index(op.f("ix_telegram_bot_sessions_state"), table_name="telegram_bot_sessions")
    op.drop_index(op.f("ix_telegram_bot_sessions_selected_service_id"), table_name="telegram_bot_sessions")
    op.drop_index(op.f("ix_telegram_bot_sessions_selected_master_id"), table_name="telegram_bot_sessions")
    op.drop_index(op.f("ix_telegram_bot_sessions_linked_customer_id"), table_name="telegram_bot_sessions")
    op.drop_index(op.f("ix_telegram_bot_sessions_last_seen_at"), table_name="telegram_bot_sessions")
    op.drop_index(op.f("ix_telegram_bot_sessions_expires_at"), table_name="telegram_bot_sessions")
    op.drop_index(op.f("ix_telegram_bot_sessions_chat_id"), table_name="telegram_bot_sessions")
    op.drop_table("telegram_bot_sessions")
