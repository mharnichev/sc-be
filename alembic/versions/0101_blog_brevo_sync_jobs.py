"""Persist future blog contact sync intents without backfilling historical rows.

Revision ID: 0101_brevo_sync_jobs
Revises: 0100_image_attempt_leases
"""
from alembic import op
import sqlalchemy as sa


revision = "0101_brevo_sync_jobs"
down_revision = "0100_image_attempt_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "blog_brevo_sync_jobs",
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("desired_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("synced_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempt_version", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("desired_version >= synced_version AND synced_version >= 0", name="blog_brevo_sync_versions_valid"),
        sa.ForeignKeyConstraint(["subscription_id"], ["blog_subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subscription_id"),
    )
    op.create_index("ix_blog_brevo_sync_jobs_next_attempt", "blog_brevo_sync_jobs", ["next_attempt_at"])


def downgrade():
    op.drop_index("ix_blog_brevo_sync_jobs_next_attempt", table_name="blog_brevo_sync_jobs")
    op.drop_table("blog_brevo_sync_jobs")
