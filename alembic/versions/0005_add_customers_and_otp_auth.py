"""add customers and otp auth

Revision ID: 0005_customers_otp
Revises: 0004_rrp_rename
Create Date: 2026-04-16 13:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_customers_otp"
down_revision = "0004_rrp_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("surname", sa.String(length=100), nullable=True),
        sa.Column("birthday", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("phone"),
    )
    op.create_index("ix_customers_phone", "customers", ["phone"], unique=True)

    op.create_table(
        "customer_otp_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_otp_codes_phone", "customer_otp_codes", ["phone"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_customer_otp_codes_phone", table_name="customer_otp_codes")
    op.drop_table("customer_otp_codes")
    op.drop_index("ix_customers_phone", table_name="customers")
    op.drop_table("customers")
