"""add monthly master schedule reminders

Revision ID: 0062_master_schedule_reminders
Revises: 0061_repeat_booking_offers
Create Date: 2026-08-30 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0062_master_schedule_reminders"
down_revision = "0061_repeat_booking_offers"
branch_labels = None
depends_on = None


message_channel = postgresql.ENUM(
    "telegram",
    "sms",
    "whatsapp",
    "email",
    name="messagechannel",
    create_type=False,
)

TEMPLATE_NAME = "Нагадування майстрам про графік"
TEMPLATE_BODY = (
    "Привіт, {master_name}! Нагадуємо відкрити робочий час на {month_name}. "
    "Зараз відкрито {coverage_percent}%."
)


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def _seed_campaign() -> None:
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
        {"name": TEMPLATE_NAME, "body": TEMPLATE_BODY},
    ).scalar_one()
    metadata_json = (
        '{"recipient":"master","trigger":"monthly_schedule_reminder",'
        '"initial_days_before_month_end":3,"initial_send_time":"10:00",'
        '"follow_up_send_time":"10:00","follow_up_window_days":3,'
        '"low_coverage_percent":30,"target_coverage_percent":50,'
        '"low_coverage_message":"За можливості збільш доступність хоча б до {target_percent}%.",'
        '"follow_up_prefix":"Повторне нагадування."}'
    )
    bind.execute(
        sa.text(
            """
            UPDATE campaigns
            SET type = 'master_schedule_reminder'::campaigntype,
                status = 'active'::campaignstatus,
                channel = 'telegram'::messagechannel,
                purpose = 'transactional'::messagepurpose,
                template_id = :template_id,
                timezone = 'Europe/Kyiv',
                metadata_json = CAST(:metadata_json AS json),
                updated_at = now()
            WHERE name = :name
            """
        ),
        {"name": TEMPLATE_NAME, "template_id": template_id, "metadata_json": metadata_json},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO campaigns (
                name, type, status, channel, purpose, template_id, timezone, metadata_json
            )
            SELECT
                :name,
                'master_schedule_reminder'::campaigntype,
                'active'::campaignstatus,
                'telegram'::messagechannel,
                'transactional'::messagepurpose,
                :template_id,
                'Europe/Kyiv',
                CAST(:metadata_json AS json)
            WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE name = :name)
            """
        ),
        {"name": TEMPLATE_NAME, "template_id": template_id, "metadata_json": metadata_json},
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE campaigntype ADD VALUE IF NOT EXISTS 'master_schedule_reminder'")

    op.create_table(
        "master_schedule_reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("calendar_master_id", sa.Integer(), nullable=True),
        sa.Column("target_month", sa.Date(), nullable=False),
        sa.Column("initial_open_minutes", sa.Integer(), nullable=True),
        sa.Column("initial_channel", message_channel, nullable=True),
        sa.Column("initial_provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("initial_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("initial_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_channel", message_channel, nullable=True),
        sa.Column("follow_up_provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("follow_up_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("follow_up_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_skip_reason", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *timestamps(),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["calendar_master_id"], ["masters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "master_id",
            "target_month",
            name="uq_master_schedule_reminders_campaign_master_month",
        ),
    )
    for column in ("campaign_id", "master_id", "calendar_master_id"):
        op.create_index(
            op.f(f"ix_master_schedule_reminders_{column}"),
            "master_schedule_reminders",
            [column],
        )
    op.create_index(
        "ix_master_schedule_reminders_target_month",
        "master_schedule_reminders",
        ["target_month"],
    )
    _seed_campaign()


def downgrade() -> None:
    op.drop_table("master_schedule_reminders")
    op.execute(sa.text("DELETE FROM campaigns WHERE name = :name").bindparams(name=TEMPLATE_NAME))
    op.execute(sa.text("DELETE FROM message_templates WHERE name = :name").bindparams(name=TEMPLATE_NAME))
