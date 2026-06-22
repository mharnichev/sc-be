"""seed sms message scenarios

Revision ID: 0032_sms_scenarios
Revises: 0031_telegram_scenarios
Create Date: 2026-06-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0032_sms_scenarios"
down_revision = "0031_telegram_scenarios"
branch_labels = None
depends_on = None


BOOKING_CONFIRMATION_BODY = (
    "Ви записані до майстра {master_name} на {appointment_date} о {appointment_time}. "
    "Чекаємо у {barbershop_name}."
)
TWO_HOUR_REMINDER_BODY = (
    "Нагадуємо, сьогодні о {appointment_time} у вас візит до майстра {master_name}. "
    "Будемо раді бачити вас у {barbershop_name}."
)


def _upsert_template(name: str, body: str) -> int:
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            INSERT INTO message_templates (name, channel, language, body, is_active)
            VALUES (:name, 'sms'::messagechannel, 'uk', :body, true)
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


def _upsert_campaign(
    *,
    name: str,
    campaign_type: str,
    template_id: int,
    location_key: str,
    metadata_json: str,
) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE campaigns
            SET type = CAST(:campaign_type AS campaigntype),
                status = 'active'::campaignstatus,
                channel = 'sms'::messagechannel,
                purpose = 'transactional'::messagepurpose,
                template_id = :template_id,
                timezone = 'Europe/Kyiv',
                location_key = :location_key,
                metadata_json = CAST(:metadata_json AS json),
                updated_at = now()
            WHERE name = :name
            """
        ),
        {
            "name": name,
            "campaign_type": campaign_type,
            "template_id": template_id,
            "location_key": location_key,
            "metadata_json": metadata_json,
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO campaigns (
                name, type, status, channel, purpose, template_id, timezone,
                location_key, metadata_json
            )
            SELECT
                :name,
                CAST(:campaign_type AS campaigntype),
                'active'::campaignstatus,
                'sms'::messagechannel,
                'transactional'::messagepurpose,
                :template_id,
                'Europe/Kyiv',
                :location_key,
                CAST(:metadata_json AS json)
            WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE name = :name)
            """
        ),
        {
            "name": name,
            "campaign_type": campaign_type,
            "template_id": template_id,
            "location_key": location_key,
            "metadata_json": metadata_json,
        },
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE campaigntype ADD VALUE IF NOT EXISTS 'booking_confirmation'")

    confirmation_template_id = _upsert_template("SMS підтвердження запису", BOOKING_CONFIRMATION_BODY)
    reminder_template_id = _upsert_template("SMS нагадування за 2 години", TWO_HOUR_REMINDER_BODY)

    _upsert_campaign(
        name="SMS підтвердження запису",
        campaign_type="booking_confirmation",
        template_id=confirmation_template_id,
        location_key="sms_booking_confirmation",
        metadata_json='{"recipient": "customer", "trigger": "booking_created"}',
    )
    _upsert_campaign(
        name="SMS нагадування за 2 години",
        campaign_type="appointment_reminder",
        template_id=reminder_template_id,
        location_key="sms_booking_two_hour_reminder",
        metadata_json='{"recipient": "customer", "trigger": "booking_upcoming", "lead_hours": 2, "window_minutes": 30}',
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM campaigns
            WHERE name IN ('SMS підтвердження запису', 'SMS нагадування за 2 години')
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM message_templates
            WHERE name IN ('SMS підтвердження запису', 'SMS нагадування за 2 години')
            """
        )
    )
