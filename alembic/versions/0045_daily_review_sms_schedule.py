"""schedule review SMS for 10:00 on the day after a visit

Revision ID: 0045_daily_review_sms
Revises: 0044_review_sms_quiet
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "0045_daily_review_sms"
down_revision = "0044_review_sms_quiet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE campaigns
        SET
            review_delay_minutes = 0,
            metadata_json = (
                COALESCE(metadata_json, '{}'::json)::jsonb
                || jsonb_build_object(
                    'schedule_mode', 'next_day',
                    'send_time', '10:00'
                )
            )::json,
            updated_at = now()
        WHERE type = 'post_visit_review_request'::campaigntype
        """
    )
    op.execute(
        """
        UPDATE message_recipients recipients
        SET scheduled_at = CASE
            WHEN (
                (
                    (bookings.end_at AT TIME ZONE 'Europe/Kyiv')::date
                    + 1
                    + time '10:00'
                ) AT TIME ZONE 'Europe/Kyiv'
            ) > now()
            THEN (
                (
                    (bookings.end_at AT TIME ZONE 'Europe/Kyiv')::date
                    + 1
                    + time '10:00'
                ) AT TIME ZONE 'Europe/Kyiv'
            )
            ELSE (
                (
                    (now() AT TIME ZONE 'Europe/Kyiv')::date
                    + 1
                    + time '10:00'
                ) AT TIME ZONE 'Europe/Kyiv'
            )
        END,
        updated_at = now()
        FROM review_requests requests
        JOIN bookings ON bookings.id = requests.appointment_id
        WHERE
            requests.recipient_id = recipients.id
            AND recipients.status = 'pending'::messagedeliverystatus
        """
    )
    op.execute(
        """
        UPDATE review_requests requests
        SET scheduled_at = recipients.scheduled_at, updated_at = now()
        FROM message_recipients recipients
        WHERE
            requests.recipient_id = recipients.id
            AND recipients.status = 'pending'::messagedeliverystatus
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE campaigns
        SET
            review_delay_minutes = 120,
            metadata_json = (
                COALESCE(metadata_json, '{}'::json)::jsonb
                - 'schedule_mode'
                - 'send_time'
            )::json,
            updated_at = now()
        WHERE type = 'post_visit_review_request'::campaigntype
        """
    )
