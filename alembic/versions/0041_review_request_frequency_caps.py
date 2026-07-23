"""set separate review request frequency caps

Revision ID: 0041_review_frequency_caps
Revises: 0040_use_sms_for_review_requests
Create Date: 2026-07-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "0041_review_frequency_caps"
down_revision = "0040_use_sms_for_review_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE campaigns SET metadata_json = ((COALESCE(metadata_json, '{}'::json))::jsonb || "
        "jsonb_build_object('frequency_cap_count', 1, 'frequency_cap_days', 90, "
        "'submitted_frequency_cap_days', 270))::json, updated_at = now() "
        "WHERE type = 'post_visit_review_request'::campaigntype"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE campaigns SET metadata_json = (((COALESCE(metadata_json, '{}'::json))::jsonb || "
        "jsonb_build_object('frequency_cap_count', 1, 'frequency_cap_days', 30)) "
        "- 'submitted_frequency_cap_days')::json, updated_at = now() "
        "WHERE type = 'post_visit_review_request'::campaigntype"
    )
