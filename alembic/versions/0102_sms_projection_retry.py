"""Persist a separate retry time for SMS outcome projection.

Revision ID: 0102_sms_projection_retry
Revises: 0101_brevo_sync_jobs
"""
from alembic import op
import sqlalchemy as sa


revision = "0102_sms_projection_retry"
down_revision = "0101_brevo_sync_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sms_queue_jobs", sa.Column("projection_retry_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("sms_queue_jobs", "projection_retry_at")
