"""add blog subscriptions

Revision ID: 0026_blog_subscriptions
Revises: 0025_telegram_contacts
Create Date: 2026-06-08 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_blog_subscriptions"
down_revision = "0025_telegram_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blog_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.Enum("subscribed", "unsubscribed", name="blogsubscriptionstatus"), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("referrer", sa.String(length=1000), nullable=True),
        sa.Column("utm_source", sa.String(length=255), nullable=True),
        sa.Column("utm_medium", sa.String(length=255), nullable=True),
        sa.Column("utm_campaign", sa.String(length=255), nullable=True),
        sa.Column("unsubscribe_token", sa.String(length=128), nullable=False),
        sa.Column("first_subscribed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subscribed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribe_reason", sa.Text(), nullable=True),
        sa.Column("subscriber_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=1000), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_blog_subscriptions_email"),
        sa.UniqueConstraint("unsubscribe_token", name="uq_blog_subscriptions_unsubscribe_token"),
    )
    op.create_index(op.f("ix_blog_subscriptions_email"), "blog_subscriptions", ["email"])
    op.create_index(op.f("ix_blog_subscriptions_status"), "blog_subscriptions", ["status"])
    op.create_index(op.f("ix_blog_subscriptions_source"), "blog_subscriptions", ["source"])
    op.create_index(op.f("ix_blog_subscriptions_language"), "blog_subscriptions", ["language"])
    op.create_index(op.f("ix_blog_subscriptions_utm_source"), "blog_subscriptions", ["utm_source"])
    op.create_index(op.f("ix_blog_subscriptions_utm_campaign"), "blog_subscriptions", ["utm_campaign"])
    op.create_index(op.f("ix_blog_subscriptions_first_subscribed_at"), "blog_subscriptions", ["first_subscribed_at"])
    op.create_index(op.f("ix_blog_subscriptions_subscribed_at"), "blog_subscriptions", ["subscribed_at"])
    op.create_index(op.f("ix_blog_subscriptions_unsubscribed_at"), "blog_subscriptions", ["unsubscribed_at"])
    op.create_index("ix_blog_subscriptions_status_created", "blog_subscriptions", ["status", "created_at"])

    op.create_table(
        "blog_subscription_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum("subscribed", "resubscribed", "unsubscribed", name="blogsubscriptioneventtype"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subscriber_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=1000), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["blog_subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_blog_subscription_events_subscription_id"), "blog_subscription_events", ["subscription_id"])
    op.create_index(op.f("ix_blog_subscription_events_event_type"), "blog_subscription_events", ["event_type"])
    op.create_index(op.f("ix_blog_subscription_events_source"), "blog_subscription_events", ["source"])
    op.create_index(op.f("ix_blog_subscription_events_occurred_at"), "blog_subscription_events", ["occurred_at"])
    op.create_index(
        "ix_blog_subscription_events_type_occurred",
        "blog_subscription_events",
        ["event_type", "occurred_at"],
    )
    op.create_index(
        "ix_blog_subscription_events_source_occurred",
        "blog_subscription_events",
        ["source", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_blog_subscription_events_source_occurred", table_name="blog_subscription_events")
    op.drop_index("ix_blog_subscription_events_type_occurred", table_name="blog_subscription_events")
    op.drop_index(op.f("ix_blog_subscription_events_occurred_at"), table_name="blog_subscription_events")
    op.drop_index(op.f("ix_blog_subscription_events_source"), table_name="blog_subscription_events")
    op.drop_index(op.f("ix_blog_subscription_events_event_type"), table_name="blog_subscription_events")
    op.drop_index(op.f("ix_blog_subscription_events_subscription_id"), table_name="blog_subscription_events")
    op.drop_table("blog_subscription_events")
    op.drop_index("ix_blog_subscriptions_status_created", table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_unsubscribed_at"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_subscribed_at"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_first_subscribed_at"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_utm_campaign"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_utm_source"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_language"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_source"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_status"), table_name="blog_subscriptions")
    op.drop_index(op.f("ix_blog_subscriptions_email"), table_name="blog_subscriptions")
    op.drop_table("blog_subscriptions")
    op.execute("DROP TYPE IF EXISTS blogsubscriptioneventtype")
    op.execute("DROP TYPE IF EXISTS blogsubscriptionstatus")
