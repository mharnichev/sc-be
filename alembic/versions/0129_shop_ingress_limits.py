"""Shared privacy-safe Shop ingress admissions, independent of business transactions."""
from alembic import op
import sqlalchemy as sa

revision = "0129_shop_ingress_limits"
down_revision = "0128_shop_payment_resolutions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shop_ingress_admissions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "action::text = ANY (ARRAY['otp_send'::text, 'otp_verify'::text, 'checkout'::text])",
            name="action",
        ),
        sa.CheckConstraint("source_key ~ '^[0-9a-f]{64}$'", name="source_key"),
    )
    op.create_index("ix_shop_ingress_source_window", "shop_ingress_admissions",
                    ["source_key", "action", "admitted_at"])


def downgrade():
    op.drop_index("ix_shop_ingress_source_window", table_name="shop_ingress_admissions")
    op.drop_table("shop_ingress_admissions")
