"""make master campaigns telegram only

Revision ID: 0064_master_telegram_only
Revises: 0063_master_lifecycle_messages
Create Date: 2026-08-31 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0064_master_telegram_only"
down_revision = "0063_master_lifecycle_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE campaigns
            SET channel = 'telegram'::messagechannel,
                metadata_json = (metadata_json::jsonb - 'fallback_to_sms')::json,
                updated_at = now()
            WHERE lower(coalesce(metadata_json->>'recipient', '')) IN ('master', 'barber')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE message_templates
            SET channel = 'telegram'::messagechannel,
                updated_at = now()
            WHERE id IN (
                SELECT template_id
                FROM campaigns
                WHERE template_id IS NOT NULL
                  AND lower(coalesce(metadata_json->>'recipient', '')) IN ('master', 'barber')
            )
            """
        )
    )


def downgrade() -> None:
    # Telegram-only is a delivery safety rule; restoring SMS cannot be inferred safely.
    pass
