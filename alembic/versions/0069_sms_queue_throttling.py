"""Durable SMSClub operation queue and account request throttle.

Revision ID: 0069_sms_queue_throttling
Revises: 0068_customer_segments
"""
from alembic import op
import sqlalchemy as sa

revision = "0069_sms_queue_throttling"
down_revision = "0068_customer_segments"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]


def upgrade():
    op.create_table(
        "sms_account_throttles",
        sa.Column("account_key", sa.String(128), primary_key=True),
        sa.Column("next_request_at", sa.DateTime(timezone=True)),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        *timestamps(),
    )
    op.create_table(
        "sms_queue_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_key", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("transport_started_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", sa.JSON()),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("delivery_status", sa.String(32)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("outcome_projected_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_sms_queue_jobs_idempotency_key"),
    )
    op.create_index("ix_sms_queue_jobs_provider_message", "sms_queue_jobs", ["account_key", "provider_message_id"])
    op.create_index("ix_sms_queue_jobs_dispatch", "sms_queue_jobs", ["account_key", "status", "priority", "available_at"])
    op.create_index("ix_sms_queue_jobs_leases", "sms_queue_jobs", ["account_key", "status", "lease_expires_at"])
    op.add_column("message_recipients", sa.Column("sms_queue_job_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_message_recipients_sms_queue_job_id_sms_queue_jobs", "message_recipients", "sms_queue_jobs", ["sms_queue_job_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_message_recipients_sms_queue_job_id", "message_recipients", ["sms_queue_job_id"])


def downgrade():
    op.drop_index("ix_message_recipients_sms_queue_job_id", table_name="message_recipients")
    op.drop_constraint("fk_message_recipients_sms_queue_job_id_sms_queue_jobs", "message_recipients", type_="foreignkey")
    op.drop_column("message_recipients", "sms_queue_job_id")
    op.drop_table("sms_queue_jobs")
    op.drop_table("sms_account_throttles")
