"""use SMS for post-visit review requests

Revision ID: 0040_use_sms_for_review_requests
Revises: 0039_verified_master_reviews
Create Date: 2026-07-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0040_use_sms_for_review_requests"
down_revision = "0039_verified_master_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE message_templates SET channel = 'sms'::messagechannel, updated_at = now() "
        "WHERE id IN (SELECT template_id FROM campaigns "
        "WHERE type = 'post_visit_review_request'::campaigntype AND template_id IS NOT NULL)"
    )
    op.execute(
        "UPDATE campaigns SET channel = 'sms'::messagechannel, "
        "location_key = COALESCE(location_key, 'sms_post_visit_review_request'), "
        "metadata_json = ((COALESCE(metadata_json, '{}'::json))::jsonb || "
        "jsonb_build_object('primary_channel', 'sms', 'fallback_channel', NULL))::json, "
        "updated_at = now() "
        "WHERE type = 'post_visit_review_request'::campaigntype"
    )
    op.execute(
        "UPDATE message_recipients AS mr SET channel = 'sms'::messagechannel, updated_at = now() "
        "FROM review_requests AS rr WHERE rr.recipient_id = mr.id "
        "AND rr.status = 'scheduled'::reviewrequeststatus "
        "AND mr.status = 'pending'::messagedeliverystatus"
    )
    op.execute(
        "UPDATE review_requests SET channel = 'sms'::messagechannel, fallback_channel = NULL, updated_at = now() "
        "WHERE status = 'scheduled'::reviewrequeststatus"
    )
    op.alter_column("review_requests", "channel", server_default=sa.text("'sms'::messagechannel"))


def downgrade() -> None:
    op.alter_column("review_requests", "channel", server_default=sa.text("'telegram'::messagechannel"))
    op.execute(
        "UPDATE campaigns SET channel = 'telegram'::messagechannel, "
        "location_key = CASE WHEN location_key = 'sms_post_visit_review_request' THEN NULL ELSE location_key END, "
        "metadata_json = ((COALESCE(metadata_json, '{}'::json))::jsonb || "
        "jsonb_build_object('primary_channel', 'telegram', 'fallback_channel', 'sms'))::json, "
        "updated_at = now() "
        "WHERE type = 'post_visit_review_request'::campaigntype"
    )
    op.execute(
        "UPDATE message_templates SET channel = 'telegram'::messagechannel, updated_at = now() "
        "WHERE id IN (SELECT template_id FROM campaigns "
        "WHERE type = 'post_visit_review_request'::campaigntype AND template_id IS NOT NULL)"
    )
