"""seed telegram message scenarios

Revision ID: 0031_telegram_scenarios
Revises: 0030_telegram_bot_sessions
Create Date: 2026-06-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0031_telegram_scenarios"
down_revision = "0030_telegram_bot_sessions"
branch_labels = None
depends_on = None


REVIEW_URL = "https://g.page/r/CXDeSQMfVvXwEBE/review"


def _upsert_template(name: str, body: str) -> int:
    bind = op.get_bind()
    return bind.execute(
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


def _upsert_campaign(
    *,
    name: str,
    campaign_type: str,
    purpose: str,
    template_id: int,
    review_url: str | None = None,
    review_delay_minutes: int | None = None,
    metadata_json: str = "{}",
) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE campaigns
            SET type = CAST(:campaign_type AS campaigntype),
                status = 'active'::campaignstatus,
                channel = 'telegram'::messagechannel,
                purpose = CAST(:purpose AS messagepurpose),
                template_id = :template_id,
                timezone = 'Europe/Kyiv',
                review_delay_minutes = :review_delay_minutes,
                review_platform = CASE WHEN :review_url IS NULL THEN review_platform ELSE 'google'::reviewplatform END,
                review_url = :review_url,
                metadata_json = CAST(:metadata_json AS json),
                updated_at = now()
            WHERE name = :name
            """
        ),
        {
            "name": name,
            "campaign_type": campaign_type,
            "purpose": purpose,
            "template_id": template_id,
            "review_url": review_url,
            "review_delay_minutes": review_delay_minutes,
            "metadata_json": metadata_json,
        },
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO campaigns (
                name, type, status, channel, purpose, template_id, timezone,
                review_delay_minutes, review_platform, review_url, metadata_json
            )
            SELECT
                :name,
                CAST(:campaign_type AS campaigntype),
                'active'::campaignstatus,
                'telegram'::messagechannel,
                CAST(:purpose AS messagepurpose),
                :template_id,
                'Europe/Kyiv',
                :review_delay_minutes,
                CASE WHEN :review_url IS NULL THEN NULL ELSE 'google'::reviewplatform END,
                :review_url,
                CAST(:metadata_json AS json)
            WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE name = :name)
            """
        ),
        {
            "name": name,
            "campaign_type": campaign_type,
            "purpose": purpose,
            "template_id": template_id,
            "review_url": review_url,
            "review_delay_minutes": review_delay_minutes,
            "metadata_json": metadata_json,
        },
    )


def upgrade() -> None:
    thanks_template_id = _upsert_template(
        "Подяка за візит",
        f"#client, Будь ласка, залиште оцінку та відгук про візит в наш барбершоп {REVIEW_URL}",
    )
    master_template_id = _upsert_template(
        "Сповіщення в момент запису",
        "Йоу! Є нова праця, збирай раму! #client #service #date",
    )
    reminder_template_id = _upsert_template(
        "Нагадування про візит",
        "#client Нагадуємо, Ви записані #date на #service",
    )

    _upsert_campaign(
        name="Подяка за візит",
        campaign_type="post_visit_review_request",
        purpose="review_request",
        template_id=thanks_template_id,
        review_url=REVIEW_URL,
        review_delay_minutes=0,
        metadata_json='{"recipient": "customer", "trigger": "booking_completed"}',
    )
    _upsert_campaign(
        name="Сповіщення в момент запису",
        campaign_type="manual",
        purpose="transactional",
        template_id=master_template_id,
        metadata_json='{"recipient": "master", "trigger": "booking_created"}',
    )
    _upsert_campaign(
        name="Нагадування про візит",
        campaign_type="appointment_reminder",
        purpose="transactional",
        template_id=reminder_template_id,
        metadata_json='{"recipient": "customer", "lead_hours": 24, "window_minutes": 60}',
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM campaigns
            WHERE name IN ('Подяка за візит', 'Сповіщення в момент запису', 'Нагадування про візит')
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM message_templates
            WHERE name IN ('Подяка за візит', 'Сповіщення в момент запису', 'Нагадування про візит')
            """
        )
    )
