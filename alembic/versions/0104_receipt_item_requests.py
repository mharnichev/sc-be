"""Persist receipt scan request identity independently of merged receipt lots.

Revision ID: 0104_receipt_item_requests
Revises: 0103_telegram_delivery_inbox
"""
from alembic import op
import sqlalchemy as sa


revision = "0104_receipt_item_requests"
down_revision = "0103_telegram_delivery_inbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "inventory_receipt_item_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id"], ["inventory_receipts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("receipt_id", "idempotency_key", name="uq_inventory_receipt_item_requests_receipt_key"),
    )


def downgrade():
    op.drop_table("inventory_receipt_item_requests")
