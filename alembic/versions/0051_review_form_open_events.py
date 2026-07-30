"""persist review form open events

Revision ID: 0051_review_form_open_events
Revises: 0050_booking_funnel_target_date
Create Date: 2026-07-29 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0051_review_form_open_events"
down_revision = "0050_booking_funnel_target_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_tracking_markers",
        sa.Column("metric_key", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("metric_key"),
    )
    op.create_table(
        "review_form_open_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_request_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            server_default="client",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["review_request_id"],
            ["review_requests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_request_id",
            name="uq_review_form_open_events_review_request_id",
        ),
    )
    # The marker is intentionally not seeded during schema rollout. Application
    # code inserts it transactionally with the first persisted form-open signal.


def downgrade() -> None:
    op.drop_table("review_form_open_events")
    op.drop_table("analytics_tracking_markers")
