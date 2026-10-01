"""enrich master booking notification

Revision ID: 0097_master_booking_notification
Revises: 0096_inventory_workflow
Create Date: 2026-10-01 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0097_master_booking_notification"
down_revision = "0096_inventory_workflow"
branch_labels = None
depends_on = None


CREATED_NAME = "Сповіщення в момент запису"
CREATED_BODY = """✅ Новий запис підтверджено

👤 Клієнт: {customer_name}
📞 Телефон: {customer_phone}
✂️ Послуги: {service_name}
📅 Дата: {appointment_date}
🕒 Час: {appointment_time}–{appointment_end_time}
{comment_line}
{promotion_line}
{price_line}

🔗 Відкрити запис в адмінці:
{booking_url}"""
PREVIOUS_BODY = (
    "Йоу! Є нова праця, збирай раму! {customer_name} {service_name} "
    "{appointment_date} {appointment_time}"
)


def _set_body(body: str) -> None:
    op.execute(
        sa.text(
            """
            UPDATE message_templates
            SET body = :body, updated_at = now()
            WHERE id = (
                SELECT template_id FROM campaigns WHERE name = :name LIMIT 1
            )
            """
        ).bindparams(body=body, name=CREATED_NAME)
    )


def upgrade() -> None:
    _set_body(CREATED_BODY)


def downgrade() -> None:
    _set_body(PREVIOUS_BODY)
