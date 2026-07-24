"""add booking funnel observability

Revision ID: 0042_booking_funnel
Revises: 0041_review_frequency_caps
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0042_booking_funnel"
down_revision = "0041_review_frequency_caps"
branch_labels = None
depends_on = None


booking_funnel_event_type = postgresql.ENUM(
    "booking_start",
    "service_selected",
    "master_selected",
    "slot_selected",
    "contact_entered",
    "booking_success",
    "no_slot",
    "stale_schedule",
    "booking_error",
    name="booking_funnel_event_type",
    create_type=False,
)
booking_funnel_event_source = postgresql.ENUM(
    "client",
    "server",
    name="booking_funnel_event_source",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    booking_funnel_event_type.create(bind, checkfirst=True)
    booking_funnel_event_source.create(bind, checkfirst=True)

    op.create_table(
        "booking_funnel_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id_hash", sa.String(length=64), nullable=False),
        sa.Column("event_type", booking_funnel_event_type, nullable=False),
        sa.Column("source", booking_funnel_event_source, nullable=False),
        sa.Column("anonymous_session_hash", sa.String(length=64), nullable=True),
        sa.Column("master_id", sa.Integer(), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["service_id"], ["barber_services.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", name="uq_booking_funnel_events_booking_id"),
        sa.UniqueConstraint(
            "event_id_hash",
            name="uq_booking_funnel_events_event_id_hash",
        ),
    )
    for column in ("event_type", "master_id", "occurred_at", "service_id"):
        op.create_index(
            op.f(f"ix_booking_funnel_events_{column}"),
            "booking_funnel_events",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_booking_funnel_events_type_occurred",
        "booking_funnel_events",
        ["event_type", "occurred_at"],
    )
    op.create_index(
        "ix_booking_funnel_events_session_type",
        "booking_funnel_events",
        ["anonymous_session_hash", "event_type"],
    )
    op.create_index(
        "ix_booking_funnel_events_master_occurred",
        "booking_funnel_events",
        ["master_id", "occurred_at"],
    )

    op.create_table(
        "booking_funnel_weekly_digests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_status", sa.String(length=32), nullable=False),
        sa.Column("insight_uk", sa.String(length=1000), nullable=False),
        sa.Column("recommended_action_code", sa.String(length=64), nullable=True),
        sa.Column("recommended_action_uk", sa.String(length=1000), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "period_start",
            "period_end",
            name="uq_booking_funnel_weekly_digests_period",
        ),
    )
    op.create_index(
        "ix_booking_funnel_weekly_digests_period_end",
        "booking_funnel_weekly_digests",
        ["period_end"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_booking_funnel_weekly_digests_period_end",
        table_name="booking_funnel_weekly_digests",
    )
    op.drop_table("booking_funnel_weekly_digests")

    op.drop_index(
        "ix_booking_funnel_events_master_occurred",
        table_name="booking_funnel_events",
    )
    op.drop_index(
        "ix_booking_funnel_events_session_type",
        table_name="booking_funnel_events",
    )
    op.drop_index(
        "ix_booking_funnel_events_type_occurred",
        table_name="booking_funnel_events",
    )
    for column in ("service_id", "occurred_at", "master_id", "event_type"):
        op.drop_index(
            op.f(f"ix_booking_funnel_events_{column}"),
            table_name="booking_funnel_events",
        )
    op.drop_table("booking_funnel_events")

    bind = op.get_bind()
    booking_funnel_event_source.drop(bind, checkfirst=True)
    booking_funnel_event_type.drop(bind, checkfirst=True)
