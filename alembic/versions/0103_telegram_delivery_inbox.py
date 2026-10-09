"""Durable Telegram ownership and successful webhook-event deduplication."""
from alembic import op
import sqlalchemy as sa

revision = "0103_telegram_delivery_inbox"
down_revision = "0102_sms_projection_retry"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]


def upgrade():
    op.create_table("telegram_delivery_attempts",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("destination", sa.String(128), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("last_error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)), *timestamps())
    op.create_table("telegram_webhook_events",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("chat_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("last_error", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)), *timestamps())
    op.create_index("ix_telegram_webhook_events_chat_id", "telegram_webhook_events", ["chat_id"])
    # A prior error does not prove non-acceptance. Preserve those historical
    # logical messages as uncertain rather than granting automatic resend.
    op.get_bind().exec_driver_sql("""
        INSERT INTO telegram_delivery_attempts
            (key, state, destination, body, attempts, started_at, last_error)
        SELECT 'recipient:' || r.id || ':telegram', 'ambiguous',
            COALESCE(p.telegram_chat_id, ''), COALESCE(r.rendered_message, ''),
            r.attempts, COALESCE(r.send_started_at, r.updated_at),
            'Historical Telegram attempt requires outcome reconciliation'
        FROM message_recipients r
        LEFT JOIN client_communication_preferences p ON p.customer_id = r.customer_id
        WHERE r.channel = 'telegram' AND r.status IN ('pending', 'failed')
            AND r.attempts > 0 AND r.provider_message_id IS NULL
        ON CONFLICT (key) DO NOTHING
    """)
    op.get_bind().exec_driver_sql("""
        INSERT INTO telegram_delivery_attempts
            (key, state, destination, body, attempts, started_at, last_error)
        SELECT d.idempotency_key, 'ambiguous', COALESCE(m.telegram_chat_id, ''),
            COALESCE(d.rendered_message, ''), d.attempts, d.updated_at,
            'Historical Telegram attempt requires outcome reconciliation'
        FROM master_message_deliveries d JOIN masters m ON m.id = d.master_id
        WHERE d.channel = 'telegram' AND d.status IN ('pending', 'failed')
            AND d.attempts > 0 AND d.provider_message_id IS NULL
        ON CONFLICT (key) DO NOTHING
    """)
    for stage, prefix in (("initial", "initial"), ("follow-up", "follow_up")):
        op.get_bind().exec_driver_sql(f"""
            INSERT INTO telegram_delivery_attempts
                (key, state, destination, body, attempts, started_at, last_error)
            SELECT 'master-schedule:' || d.campaign_id || ':' || d.master_id || ':' ||
                d.target_month || ':{stage}', 'ambiguous', COALESCE(m.telegram_chat_id, ''),
                '', d.{prefix}_attempts, d.updated_at,
                'Historical Telegram attempt requires outcome reconciliation'
            FROM master_schedule_reminders d JOIN masters m ON m.id = d.master_id
            WHERE d.{prefix}_attempts > 0 AND d.{prefix}_sent_at IS NULL
                AND d.{prefix}_provider_message_id IS NULL
                AND (d.{prefix}_channel IS NULL OR d.{prefix}_channel = 'telegram')
            ON CONFLICT (key) DO NOTHING
        """)


def downgrade():
    op.drop_table("telegram_webhook_events")
    op.drop_table("telegram_delivery_attempts")
