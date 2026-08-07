"""add opaque customer activity links and message waitlist relations

Revision ID: 0056_customer_activity
Revises: 0055_no_slots_waitlist
Create Date: 2026-08-06 00:00:00.000000
"""

from __future__ import annotations

from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "0056_customer_activity"
down_revision = "0055_no_slots_waitlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("public_id", sa.String(length=36), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM bookings WHERE public_id IS NULL")).all()
    for row in rows:
        bind.execute(
            sa.text("UPDATE bookings SET public_id = :public_id WHERE id = :id"),
            {"id": row.id, "public_id": str(uuid4())},
        )
    op.alter_column("bookings", "public_id", nullable=False)
    op.create_index(op.f("ix_bookings_public_id"), "bookings", ["public_id"], unique=True)

    op.create_table(
        "customer_activity_access_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_booking_id", sa.Integer(), nullable=True),
        sa.Column("source_waitlist_request_id", sa.Integer(), nullable=True),
        sa.Column("recipient_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_waitlist_request_id"], ["waitlist_requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipient_id"], ["message_recipients.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_customer_activity_access_tokens_token_hash"),
    )
    op.create_index(op.f("ix_customer_activity_access_tokens_token_hash"), "customer_activity_access_tokens", ["token_hash"])
    op.create_index(op.f("ix_customer_activity_access_tokens_customer_id"), "customer_activity_access_tokens", ["customer_id"])
    op.create_index(op.f("ix_customer_activity_access_tokens_expires_at"), "customer_activity_access_tokens", ["expires_at"])
    op.create_index(op.f("ix_customer_activity_access_tokens_revoked_at"), "customer_activity_access_tokens", ["revoked_at"])
    op.create_index(op.f("ix_customer_activity_access_tokens_source_booking_id"), "customer_activity_access_tokens", ["source_booking_id"])
    op.create_index(op.f("ix_customer_activity_access_tokens_source_waitlist_request_id"), "customer_activity_access_tokens", ["source_waitlist_request_id"])
    op.create_index(op.f("ix_customer_activity_access_tokens_recipient_id"), "customer_activity_access_tokens", ["recipient_id"])
    op.create_index("ix_customer_activity_access_tokens_customer_active", "customer_activity_access_tokens", ["customer_id", "expires_at"])

    for table in ("message_recipients", "message_logs"):
        op.add_column(table, sa.Column("waitlist_request_id", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("waitlist_offer_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_waitlist_request_id", table, "waitlist_requests", ["waitlist_request_id"], ["id"], ondelete="SET NULL"
        )
        op.create_foreign_key(
            f"fk_{table}_waitlist_offer_id", table, "waitlist_offers", ["waitlist_offer_id"], ["id"], ondelete="SET NULL"
        )
        op.create_index(op.f(f"ix_{table}_waitlist_request_id"), table, ["waitlist_request_id"])
        op.create_index(op.f(f"ix_{table}_waitlist_offer_id"), table, ["waitlist_offer_id"])

    # This system campaign is visible in backoffice Message Recipients/Logs but
    # its stored body never contains the raw fragment capability.
    op.execute(
        sa.text(
            """
            INSERT INTO campaigns (name, type, status, channel, purpose, timezone, location_key, metadata_json)
            SELECT 'SMS додано до листа очікування', 'manual'::campaigntype,
                   'active'::campaignstatus, 'sms'::messagechannel,
                   'transactional'::messagepurpose, 'Europe/Kyiv',
                   'sms_waitlist_created', CAST('{"system": "customer_activity"}' AS json)
            WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE location_key = 'sms_waitlist_created')
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO campaigns (name, type, status, channel, purpose, timezone, location_key, metadata_json)
            SELECT 'SMS пропозиція з листа очікування', 'manual'::campaigntype,
                   'active'::campaignstatus, 'sms'::messagechannel,
                   'transactional'::messagepurpose, 'Europe/Kyiv',
                   'sms_waitlist_offer', CAST('{"system": "waitlist_offer"}' AS json)
            WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE location_key = 'sms_waitlist_offer')
            """
        )
    )


def downgrade() -> None:
    # Preserve any pre-existing operator-owned campaign that happened to use
    # these location keys; remove only rows seeded by this migration.
    op.execute(
        sa.text(
            """
            DELETE FROM campaigns
            WHERE location_key IN ('sms_waitlist_created', 'sms_waitlist_offer')
              AND metadata_json ->> 'system' IN ('customer_activity', 'waitlist_offer')
            """
        )
    )
    for table in ("message_logs", "message_recipients"):
        op.drop_index(op.f(f"ix_{table}_waitlist_offer_id"), table_name=table)
        op.drop_index(op.f(f"ix_{table}_waitlist_request_id"), table_name=table)
        op.drop_constraint(f"fk_{table}_waitlist_offer_id", table, type_="foreignkey")
        op.drop_constraint(f"fk_{table}_waitlist_request_id", table, type_="foreignkey")
        op.drop_column(table, "waitlist_offer_id")
        op.drop_column(table, "waitlist_request_id")
    op.drop_table("customer_activity_access_tokens")
    op.drop_index(op.f("ix_bookings_public_id"), table_name="bookings")
    op.drop_column("bookings", "public_id")
