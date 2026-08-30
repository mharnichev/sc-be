"""add campaign-backed master booking lifecycle messages

Revision ID: 0063_master_lifecycle_messages
Revises: 0062_master_schedule_reminders
Create Date: 2026-08-30 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0063_master_lifecycle_messages"
down_revision = "0062_master_schedule_reminders"
branch_labels = None
depends_on = None


message_channel = postgresql.ENUM(
    "telegram", "sms", "whatsapp", "email", name="messagechannel", create_type=False
)
message_delivery_status = postgresql.ENUM(
    "pending", "sent", "delivered", "failed", "skipped",
    name="messagedeliverystatus",
    create_type=False,
)

CREATED_NAME = "Сповіщення в момент запису"
CANCELLED_NAME = "Сповіщення про скасування запису"
CREATED_BODY = (
    "Йоу! Є нова праця, збирай раму! {customer_name} {service_name} "
    "{appointment_date} {appointment_time}"
)
CANCELLED_BODY = (
    "❗ Клієнт {customer_name} скасував запис: {service_name} "
    "{appointment_date} {appointment_time}"
)


def _upsert_scenario(*, name: str, campaign_type: str, trigger: str, body: str) -> None:
    bind = op.get_bind()
    template_id = bind.execute(
        sa.text(
            """
            INSERT INTO message_templates (name, channel, language, body, is_active)
            VALUES (:name, 'telegram'::messagechannel, 'uk', :body, true)
            ON CONFLICT (name) DO UPDATE
            SET channel = EXCLUDED.channel,
                language = EXCLUDED.language,
                body = EXCLUDED.body,
                is_active = EXCLUDED.is_active,
                updated_at = now()
            RETURNING id
            """
        ),
        {"name": name, "body": body},
    ).scalar_one()
    metadata_json = (
        '{"recipient":"master","trigger":"' + trigger + '","fallback_to_sms":true}'
    )
    result = bind.execute(
        sa.text(
            """
            UPDATE campaigns
            SET type = CAST(:campaign_type AS campaigntype),
                status = 'active'::campaignstatus,
                channel = 'telegram'::messagechannel,
                purpose = 'transactional'::messagepurpose,
                template_id = :template_id,
                timezone = 'Europe/Kyiv',
                location_key = :trigger,
                metadata_json = CAST(:metadata_json AS json),
                updated_at = now()
            WHERE name = :name
            """
        ),
        {
            "name": name,
            "campaign_type": campaign_type,
            "template_id": template_id,
            "trigger": trigger,
            "metadata_json": metadata_json,
        },
    )
    if result.rowcount:
        return
    bind.execute(
        sa.text(
            """
            INSERT INTO campaigns (
                name, type, status, channel, purpose, template_id, timezone,
                location_key, metadata_json
            ) VALUES (
                :name, CAST(:campaign_type AS campaigntype),
                'active'::campaignstatus, 'telegram'::messagechannel,
                'transactional'::messagepurpose, :template_id, 'Europe/Kyiv',
                :trigger, CAST(:metadata_json AS json)
            )
            """
        ),
        {
            "name": name,
            "campaign_type": campaign_type,
            "template_id": template_id,
            "trigger": trigger,
            "metadata_json": metadata_json,
        },
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE campaigntype ADD VALUE IF NOT EXISTS 'master_booking_created'")
        op.execute("ALTER TYPE campaigntype ADD VALUE IF NOT EXISTS 'master_booking_cancelled'")

    op.create_table(
        "master_message_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("channel", message_channel, nullable=False),
        sa.Column("status", message_delivery_status, server_default="pending", nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("rendered_message", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_master_message_deliveries_idempotency_key"),
    )
    for column in ("campaign_id", "master_id", "booking_id", "trigger", "status"):
        op.create_index(
            op.f(f"ix_master_message_deliveries_{column}"),
            "master_message_deliveries",
            [column],
        )
    op.create_index(
        "ix_master_message_deliveries_campaign_status",
        "master_message_deliveries",
        ["campaign_id", "status"],
    )
    _upsert_scenario(
        name=CREATED_NAME,
        campaign_type="master_booking_created",
        trigger="booking_created",
        body=CREATED_BODY,
    )
    _upsert_scenario(
        name=CANCELLED_NAME,
        campaign_type="master_booking_cancelled",
        trigger="booking_cancelled",
        body=CANCELLED_BODY,
    )


def downgrade() -> None:
    op.drop_table("master_message_deliveries")
    op.execute(sa.text("DELETE FROM campaigns WHERE name = :name").bindparams(name=CANCELLED_NAME))
    op.execute(sa.text("DELETE FROM message_templates WHERE name = :name").bindparams(name=CANCELLED_NAME))
    op.execute(
        sa.text(
            """
            UPDATE campaigns
            SET type = 'manual'::campaigntype,
                location_key = NULL,
                metadata_json = '{"recipient":"master","trigger":"booking_created"}'::json
            WHERE name = :name
            """
        ).bindparams(name=CREATED_NAME)
    )
