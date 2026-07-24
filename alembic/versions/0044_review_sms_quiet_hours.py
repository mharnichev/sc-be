"""set review SMS quiet hours to 20:00-10:00 Kyiv

Revision ID: 0044_review_sms_quiet
Revises: 0043_full_client_consent
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "0044_review_sms_quiet"
down_revision = "0043_full_client_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE campaigns
        SET
            metadata_json = (
                COALESCE(metadata_json, '{}'::json)::jsonb
                || jsonb_build_object(
                    'quiet_hours_enabled', true,
                    'quiet_hours_from', '20:00',
                    'quiet_hours_to', '10:00'
                )
            )::json,
            updated_at = now()
        WHERE type = 'post_visit_review_request'::campaigntype
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE campaigns
        SET
            metadata_json = (
                COALESCE(metadata_json, '{}'::json)::jsonb
                || jsonb_build_object(
                    'quiet_hours_enabled', true,
                    'quiet_hours_from', '21:00',
                    'quiet_hours_to', '09:00'
                )
            )::json,
            updated_at = now()
        WHERE type = 'post_visit_review_request'::campaigntype
        """
    )
