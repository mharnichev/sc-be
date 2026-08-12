"""make booking confirmation activity links template-managed

Revision ID: 0060_booking_sms_links
Revises: 0059_redirect_calendar_repair
Create Date: 2026-08-11 00:00:00.000000
"""

from __future__ import annotations

import json
import re

from alembic import op
import sqlalchemy as sa


revision = "0060_booking_sms_links"
down_revision = "0059_redirect_calendar_repair"
branch_labels = None
depends_on = None


LEGACY_BODY = (
    "Ви записані до майстра {master_name} на {appointment_date} о {appointment_time}. "
    "Чекаємо у {barbershop_name}."
)
MANAGE_LINE = "Переглянути: {manage_url}"
CANCEL_LINE = "Скасувати: {cancel_url}"


def ensure_activity_links(body: str) -> str:
    result = body.rstrip()
    if "manage_url" not in _variables(result):
        result = f"{result}\n{MANAGE_LINE}"
    if "cancel_url" not in _variables(result):
        result = f"{result}\n{CANCEL_LINE}"
    return result


def _variables(body: str) -> set[str]:
    result: set[str] = set()
    for name in ("manage_url", "cancel_url"):
        escaped = re.escape(name)
        patterns = (
            r"\{\{\s*" + escaped + r"\s*\}\}",
            r"(?<!\{)\{\s*" + escaped + r"\s*\}(?!\})",
            r"(?<![\w/])#" + escaped + r"\b",
        )
        if any(re.search(pattern, body) for pattern in patterns):
            result.add(name)
    return result


def _metadata_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        loaded = json.loads(value)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def upgrade() -> None:
    bind = op.get_bind()
    campaigns = bind.execute(
        sa.text(
            """
            SELECT c.id, c.metadata_json, t.body AS template_body
            FROM campaigns AS c
            LEFT JOIN message_templates AS t ON t.id = c.template_id
            WHERE c.channel = 'sms'::messagechannel
              AND (
                c.type = 'booking_confirmation'::campaigntype
                OR c.location_key = 'sms_booking_confirmation'
              )
            """
        )
    ).mappings()
    for row in campaigns:
        metadata = _metadata_dict(row["metadata_json"])
        metadata_body = metadata.get("message_body")
        source_body = (
            metadata_body
            if isinstance(metadata_body, str) and metadata_body.strip()
            else row["template_body"] or LEGACY_BODY
        )
        metadata["message_body"] = ensure_activity_links(source_body)
        bind.execute(
            sa.text(
                """
                UPDATE campaigns
                SET metadata_json = CAST(:metadata_json AS json),
                    updated_at = now()
                WHERE id = :campaign_id
                """
            ),
            {
                "campaign_id": row["id"],
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
            },
        )

    standard_templates = bind.execute(
        sa.text(
            """
            SELECT id, body
            FROM message_templates
            WHERE channel = 'sms'::messagechannel
              AND name = 'SMS підтвердження запису'
            """
        )
    ).mappings()
    for row in standard_templates:
        bind.execute(
            sa.text(
                """
                UPDATE message_templates
                SET body = :body, updated_at = now()
                WHERE id = :template_id
                """
            ),
            {
                "template_id": row["id"],
                "body": ensure_activity_links(row["body"]),
            },
        )


def downgrade() -> None:
    # Operator-managed message copy may change after this migration. Removing
    # placeholders on downgrade could silently destroy those edits, so the data
    # migration is intentionally preserved.
    pass
