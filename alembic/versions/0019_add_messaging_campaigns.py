"""add messaging campaigns

Revision ID: 0019_messaging_campaigns
Revises: 0018_booking_service_items
Create Date: 2026-05-30 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_messaging_campaigns"
down_revision = "0018_booking_service_items"
branch_labels = None
depends_on = None


campaign_type = postgresql.ENUM(
    "manual",
    "post_visit_review_request",
    "appointment_reminder",
    "birthday_greeting",
    "re_engagement",
    "first_visit_follow_up",
    "loyalty_vip",
    name="campaigntype",
    create_type=False,
)
campaign_status = postgresql.ENUM("draft", "active", "paused", "completed", "archived", name="campaignstatus", create_type=False)
message_channel = postgresql.ENUM("telegram", "sms", "whatsapp", "email", name="messagechannel", create_type=False)
message_delivery_status = postgresql.ENUM("pending", "sent", "failed", "skipped", name="messagedeliverystatus", create_type=False)
message_purpose = postgresql.ENUM("marketing", "transactional", "review_request", name="messagepurpose", create_type=False)
consent_status = postgresql.ENUM("unknown", "opted_in", "opted_out", name="consentstatus", create_type=False)
review_platform = postgresql.ENUM("google", "instagram", "internal", "custom", name="reviewplatform", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (
        campaign_type,
        campaign_status,
        message_channel,
        message_delivery_status,
        message_purpose,
        consent_status,
        review_platform,
    ):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("channel", message_channel, nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_message_templates_name")),
    )
    op.create_index(op.f("ix_message_templates_language"), "message_templates", ["language"], unique=False)
    op.create_index(op.f("ix_message_templates_name"), "message_templates", ["name"], unique=False)

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", campaign_type, nullable=False),
        sa.Column("status", campaign_status, nullable=False),
        sa.Column("channel", message_channel, nullable=False),
        sa.Column("purpose", message_purpose, nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("review_delay_minutes", sa.Integer(), nullable=True),
        sa.Column("follow_up_delay_days", sa.Integer(), nullable=True),
        sa.Column("review_platform", review_platform, nullable=True),
        sa.Column("review_url", sa.String(length=1000), nullable=True),
        sa.Column("discount_code", sa.String(length=100), nullable=True),
        sa.Column("location_key", sa.String(length=100), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["message_templates.id"], name=op.f("fk_campaigns_template_id_message_templates"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaigns")),
    )
    op.create_index(op.f("ix_campaigns_location_key"), "campaigns", ["location_key"], unique=False)
    op.create_index(op.f("ix_campaigns_name"), "campaigns", ["name"], unique=False)
    op.create_index(op.f("ix_campaigns_scheduled_at"), "campaigns", ["scheduled_at"], unique=False)
    op.create_index(op.f("ix_campaigns_status"), "campaigns", ["status"], unique=False)
    op.create_index(op.f("ix_campaigns_template_id"), "campaigns", ["template_id"], unique=False)
    op.create_index(op.f("ix_campaigns_type"), "campaigns", ["type"], unique=False)

    op.create_table(
        "campaign_audience_filters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_campaign_audience_filters_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_audience_filters")),
        sa.UniqueConstraint("campaign_id", name=op.f("uq_campaign_audience_filters_campaign_id")),
    )
    op.create_index(op.f("ix_campaign_audience_filters_campaign_id"), "campaign_audience_filters", ["campaign_id"], unique=False)

    op.create_table(
        "client_communication_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=128), nullable=True),
        sa.Column("preferred_language", sa.String(length=16), nullable=True),
        sa.Column("marketing_consent", consent_status, nullable=False),
        sa.Column("transactional_consent", consent_status, nullable=False),
        sa.Column("do_not_contact", sa.Boolean(), nullable=False),
        sa.Column("blacklisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_client_communication_preferences_customer_id_customers"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_communication_preferences")),
        sa.UniqueConstraint("customer_id", name=op.f("uq_client_communication_preferences_customer_id")),
    )
    op.create_index(op.f("ix_client_communication_preferences_customer_id"), "client_communication_preferences", ["customer_id"], unique=False)
    op.create_index(op.f("ix_client_communication_preferences_telegram_chat_id"), "client_communication_preferences", ["telegram_chat_id"], unique=False)

    op.create_table(
        "channel_provider_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel", message_channel, nullable=False),
        sa.Column("provider_name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_provider_configs")),
        sa.UniqueConstraint("channel", "provider_name", name="uq_channel_provider_configs_channel_provider"),
    )
    op.create_index(op.f("ix_channel_provider_configs_channel"), "channel_provider_configs", ["channel"], unique=False)

    op.create_table(
        "message_recipients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("channel", message_channel, nullable=False),
        sa.Column("status", message_delivery_status, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rendered_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["appointment_id"], ["bookings.id"], name=op.f("fk_message_recipients_appointment_id_bookings"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_message_recipients_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_message_recipients_customer_id_customers"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_recipients")),
        sa.UniqueConstraint("idempotency_key", name="uq_message_recipients_idempotency_key"),
    )
    op.create_index(op.f("ix_message_recipients_appointment_id"), "message_recipients", ["appointment_id"], unique=False)
    op.create_index(op.f("ix_message_recipients_campaign_id"), "message_recipients", ["campaign_id"], unique=False)
    op.create_index("ix_message_recipients_campaign_status", "message_recipients", ["campaign_id", "status"], unique=False)
    op.create_index(op.f("ix_message_recipients_customer_id"), "message_recipients", ["customer_id"], unique=False)
    op.create_index(op.f("ix_message_recipients_next_retry_at"), "message_recipients", ["next_retry_at"], unique=False)
    op.create_index(op.f("ix_message_recipients_scheduled_at"), "message_recipients", ["scheduled_at"], unique=False)
    op.create_index(op.f("ix_message_recipients_status"), "message_recipients", ["status"], unique=False)

    op.create_table(
        "message_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("recipient_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("channel", message_channel, nullable=False),
        sa.Column("status", message_delivery_status, nullable=False),
        sa.Column("provider_response", sa.JSON(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["appointment_id"], ["bookings.id"], name=op.f("fk_message_logs_appointment_id_bookings"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_message_logs_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_message_logs_customer_id_customers"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["message_recipients.id"], name=op.f("fk_message_logs_recipient_id_message_recipients"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_logs")),
    )
    op.create_index(op.f("ix_message_logs_appointment_id"), "message_logs", ["appointment_id"], unique=False)
    op.create_index(op.f("ix_message_logs_campaign_id"), "message_logs", ["campaign_id"], unique=False)
    op.create_index(op.f("ix_message_logs_customer_id"), "message_logs", ["customer_id"], unique=False)
    op.create_index(op.f("ix_message_logs_recipient_id"), "message_logs", ["recipient_id"], unique=False)
    op.create_index(op.f("ix_message_logs_status"), "message_logs", ["status"], unique=False)

    op.create_table(
        "review_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("platform", review_platform, nullable=False),
        sa.Column("review_url", sa.String(length=1000), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipient_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["appointment_id"], ["bookings.id"], name=op.f("fk_review_requests_appointment_id_bookings"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_review_requests_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], name=op.f("fk_review_requests_customer_id_customers"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["message_recipients.id"], name=op.f("fk_review_requests_recipient_id_message_recipients"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_requests")),
        sa.UniqueConstraint("campaign_id", "appointment_id", name="uq_review_requests_campaign_appointment"),
    )
    op.create_index(op.f("ix_review_requests_appointment_id"), "review_requests", ["appointment_id"], unique=False)
    op.create_index(op.f("ix_review_requests_campaign_id"), "review_requests", ["campaign_id"], unique=False)
    op.create_index(op.f("ix_review_requests_customer_id"), "review_requests", ["customer_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_review_requests_customer_id"), table_name="review_requests")
    op.drop_index(op.f("ix_review_requests_campaign_id"), table_name="review_requests")
    op.drop_index(op.f("ix_review_requests_appointment_id"), table_name="review_requests")
    op.drop_table("review_requests")
    op.drop_index(op.f("ix_message_logs_status"), table_name="message_logs")
    op.drop_index(op.f("ix_message_logs_recipient_id"), table_name="message_logs")
    op.drop_index(op.f("ix_message_logs_customer_id"), table_name="message_logs")
    op.drop_index(op.f("ix_message_logs_campaign_id"), table_name="message_logs")
    op.drop_index(op.f("ix_message_logs_appointment_id"), table_name="message_logs")
    op.drop_table("message_logs")
    op.drop_index(op.f("ix_message_recipients_status"), table_name="message_recipients")
    op.drop_index(op.f("ix_message_recipients_scheduled_at"), table_name="message_recipients")
    op.drop_index(op.f("ix_message_recipients_next_retry_at"), table_name="message_recipients")
    op.drop_index(op.f("ix_message_recipients_customer_id"), table_name="message_recipients")
    op.drop_index("ix_message_recipients_campaign_status", table_name="message_recipients")
    op.drop_index(op.f("ix_message_recipients_campaign_id"), table_name="message_recipients")
    op.drop_index(op.f("ix_message_recipients_appointment_id"), table_name="message_recipients")
    op.drop_table("message_recipients")
    op.drop_index(op.f("ix_channel_provider_configs_channel"), table_name="channel_provider_configs")
    op.drop_table("channel_provider_configs")
    op.drop_index(op.f("ix_client_communication_preferences_telegram_chat_id"), table_name="client_communication_preferences")
    op.drop_index(op.f("ix_client_communication_preferences_customer_id"), table_name="client_communication_preferences")
    op.drop_table("client_communication_preferences")
    op.drop_index(op.f("ix_campaign_audience_filters_campaign_id"), table_name="campaign_audience_filters")
    op.drop_table("campaign_audience_filters")
    op.drop_index(op.f("ix_campaigns_type"), table_name="campaigns")
    op.drop_index(op.f("ix_campaigns_template_id"), table_name="campaigns")
    op.drop_index(op.f("ix_campaigns_status"), table_name="campaigns")
    op.drop_index(op.f("ix_campaigns_scheduled_at"), table_name="campaigns")
    op.drop_index(op.f("ix_campaigns_name"), table_name="campaigns")
    op.drop_index(op.f("ix_campaigns_location_key"), table_name="campaigns")
    op.drop_table("campaigns")
    op.drop_index(op.f("ix_message_templates_name"), table_name="message_templates")
    op.drop_index(op.f("ix_message_templates_language"), table_name="message_templates")
    op.drop_table("message_templates")

    bind = op.get_bind()
    for enum_type in (
        review_platform,
        consent_status,
        message_purpose,
        message_delivery_status,
        message_channel,
        campaign_status,
        campaign_type,
    ):
        enum_type.drop(bind, checkfirst=True)
