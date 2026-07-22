"""add verified master reviews

Revision ID: 0039_verified_master_reviews
Revises: 0038_add_brand_logo_url
Create Date: 2026-07-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0039_verified_master_reviews"
down_revision = "0038_add_brand_logo_url"
branch_labels = None
depends_on = None


master_review_status = postgresql.ENUM(
    "pending", "approved", "rejected", name="masterreviewstatus", create_type=False
)
review_request_status = postgresql.ENUM(
    "scheduled", "sent", "delivered", "submitted", "expired", "failed",
    name="reviewrequeststatus",
    create_type=False,
)
message_channel = postgresql.ENUM(
    "telegram", "sms", "whatsapp", "email", name="messagechannel", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    master_review_status.create(bind, checkfirst=True)
    review_request_status.create(bind, checkfirst=True)

    op.create_table(
        "master_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", master_review_status, server_default="pending", nullable=False),
        sa.Column("public_author_name", sa.String(length=100), server_default="Verified client", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("moderated_by", sa.Integer(), nullable=True),
        sa.Column("moderation_reason", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name=op.f("ck_master_reviews_master_reviews_rating_range")),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["moderated_by"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id"),
    )
    for column in ("booking_id", "customer_id", "master_id", "moderated_by", "published_at", "rating", "status", "submitted_at"):
        op.create_index(op.f(f"ix_master_reviews_{column}"), "master_reviews", [column], unique=False)
    op.create_index(
        "ix_master_reviews_master_status_submitted",
        "master_reviews",
        ["master_id", "status", "submitted_at"],
        unique=False,
    )

    op.create_table(
        "master_review_moderation_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("from_status", master_review_status, nullable=False),
        sa.Column("to_status", master_review_status, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["admin_users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_id"], ["master_reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_master_review_moderation_audits_actor_id"), "master_review_moderation_audits", ["actor_id"])
    op.create_index(op.f("ix_master_review_moderation_audits_review_id"), "master_review_moderation_audits", ["review_id"])
    op.create_index(
        "ix_master_review_audits_review_created",
        "master_review_moderation_audits",
        ["review_id", "created_at"],
    )

    op.add_column("review_requests", sa.Column("master_id", sa.Integer(), nullable=True))
    op.add_column("review_requests", sa.Column("review_id", sa.Integer(), nullable=True))
    op.add_column("review_requests", sa.Column("token_hash", sa.String(length=64), nullable=True))
    op.add_column("review_requests", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("review_requests", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("review_requests", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("review_requests", sa.Column("channel", message_channel, server_default="telegram", nullable=False))
    op.add_column("review_requests", sa.Column("fallback_channel", message_channel, nullable=True))
    op.add_column(
        "review_requests",
        sa.Column("status", review_request_status, server_default="scheduled", nullable=False),
    )
    op.add_column("review_requests", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.execute(
        "UPDATE review_requests AS rr SET master_id = b.master_id, scheduled_at = rr.created_at "
        "FROM bookings AS b WHERE b.id = rr.appointment_id"
    )
    op.execute(
        "UPDATE review_requests SET status = CASE "
        "WHEN reviewed_at IS NOT NULL THEN 'submitted'::reviewrequeststatus "
        "WHEN sent_at IS NOT NULL THEN 'sent'::reviewrequeststatus "
        "ELSE 'scheduled'::reviewrequeststatus END"
    )
    op.alter_column("review_requests", "master_id", nullable=False)
    op.create_foreign_key(None, "review_requests", "masters", ["master_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(None, "review_requests", "master_reviews", ["review_id"], ["id"], ondelete="SET NULL")
    for column in ("expires_at", "master_id", "review_id", "scheduled_at", "status", "token_hash"):
        op.create_index(op.f(f"ix_review_requests_{column}"), "review_requests", [column], unique=column in {"review_id", "token_hash"})
    op.create_unique_constraint("uq_review_requests_appointment", "review_requests", ["appointment_id"])

    op.create_table(
        "review_request_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_request_id", sa.Integer(), nullable=False),
        sa.Column("status", review_request_status, nullable=False),
        sa.Column("channel", message_channel, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_request_id"], ["review_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_review_request_events_review_request_id"), "review_request_events", ["review_request_id"])
    op.create_index(op.f("ix_review_request_events_status"), "review_request_events", ["status"])
    op.create_index(
        "ix_review_request_events_request_created", "review_request_events", ["review_request_id", "created_at"]
    )
    op.execute(
        "INSERT INTO review_request_events (review_request_id, status, channel, created_at, updated_at) "
        "SELECT id, status, channel, COALESCE(scheduled_at, created_at), COALESCE(scheduled_at, created_at) "
        "FROM review_requests"
    )
    op.execute(
        "UPDATE message_templates SET "
        "body = '#client, дякуємо за візит до Soul Cuts. Залиште чесний відгук про роботу майстра: {{review_link}}', "
        "updated_at = now() "
        "WHERE id IN (SELECT template_id FROM campaigns "
        "WHERE type = 'post_visit_review_request'::campaigntype AND template_id IS NOT NULL)"
    )
    op.execute(
        "UPDATE campaigns SET review_delay_minutes = 120, "
        "review_platform = 'internal'::reviewplatform, review_url = NULL, timezone = 'Europe/Kyiv', "
        "metadata_json = ((COALESCE(metadata_json, '{}'::json))::jsonb || "
        "jsonb_build_object('primary_channel', 'telegram', 'fallback_channel', 'sms', "
        "'quiet_hours_enabled', true, 'quiet_hours_from', '21:00', 'quiet_hours_to', '09:00', "
        "'frequency_cap_count', 1, 'frequency_cap_days', 30, 'exclusions', '{}'::jsonb))::json, updated_at = now() "
        "WHERE type = 'post_visit_review_request'::campaigntype"
    )


def downgrade() -> None:
    op.drop_index("ix_review_request_events_request_created", table_name="review_request_events")
    op.drop_index(op.f("ix_review_request_events_status"), table_name="review_request_events")
    op.drop_index(op.f("ix_review_request_events_review_request_id"), table_name="review_request_events")
    op.drop_table("review_request_events")

    op.drop_constraint("uq_review_requests_appointment", "review_requests", type_="unique")
    for column in ("token_hash", "status", "scheduled_at", "review_id", "master_id", "expires_at"):
        op.drop_index(op.f(f"ix_review_requests_{column}"), table_name="review_requests")
    op.drop_constraint(op.f("fk_review_requests_review_id_master_reviews"), "review_requests", type_="foreignkey")
    op.drop_constraint(op.f("fk_review_requests_master_id_masters"), "review_requests", type_="foreignkey")
    for column in (
        "failure_reason", "status", "fallback_channel", "channel", "delivered_at", "scheduled_at",
        "expires_at", "token_hash", "review_id", "master_id",
    ):
        op.drop_column("review_requests", column)

    op.drop_index("ix_master_review_audits_review_created", table_name="master_review_moderation_audits")
    op.drop_index(op.f("ix_master_review_moderation_audits_review_id"), table_name="master_review_moderation_audits")
    op.drop_index(op.f("ix_master_review_moderation_audits_actor_id"), table_name="master_review_moderation_audits")
    op.drop_table("master_review_moderation_audits")
    op.drop_index("ix_master_reviews_master_status_submitted", table_name="master_reviews")
    for column in ("submitted_at", "status", "rating", "published_at", "moderated_by", "master_id", "customer_id", "booking_id"):
        op.drop_index(op.f(f"ix_master_reviews_{column}"), table_name="master_reviews")
    op.drop_table("master_reviews")

    bind = op.get_bind()
    review_request_status.drop(bind, checkfirst=True)
    master_review_status.drop(bind, checkfirst=True)
