"""Revoke other customer sessions after a verified phone change.

Revision ID: 0105_customer_sessions
Revises: 0104_receipt_item_requests
"""
from alembic import op
import sqlalchemy as sa

revision = "0105_customer_sessions"
down_revision = "0104_receipt_item_requests"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("customers", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("customers", sa.Column("retained_access_token_hash", sa.String(64), nullable=True))
    op.add_column("customer_activity_access_tokens", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("customer_activity_access_tokens", "session_version")
    op.drop_column("customers", "retained_access_token_hash")
    op.drop_column("customers", "session_version")
