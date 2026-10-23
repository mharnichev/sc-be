"""Opt-in, one-shot checkout service reminders; no historical enrollment."""
from alembic import op
import sqlalchemy as sa

revision = "0142_checkout_reminders"
down_revision = "0141_product_shop_price"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f("ck_shop_ingress_admissions_action"), "shop_ingress_admissions", type_="check")
    op.create_check_constraint(op.f("ck_shop_ingress_admissions_action"), "shop_ingress_admissions",
        "action IN ('otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart', 'reminder', 'reminder_email')")
    op.create_table("checkout_reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("management_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="CASCADE")),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="SET NULL")),
        sa.Column("email", sa.String(255)), sa.Column("email_hash", sa.String(64)),
        sa.Column("locale", sa.String(2), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True)), sa.Column("consent_version", sa.String(32)),
        sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("payload_hash", sa.String(64)),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("recovery_management_hash", sa.String(64), unique=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(32)), sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('pending','attempting','retry','sent','dry_run','uncertain','cancelled','completed','expired','suppressed')", name="checkout_reminder_state"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 3", name="checkout_reminder_attempts"))
    op.create_index("ix_checkout_reminders_due", "checkout_reminders", ["state", "due_at"])
    op.create_index("ix_checkout_reminders_email_hash", "checkout_reminders", ["email_hash"])
    op.create_index("ix_checkout_reminders_expires_at", "checkout_reminders", ["expires_at"])


def downgrade():
    op.drop_table("checkout_reminders")
    # Ephemeral admission counters contain no business history.
    op.execute("DELETE FROM shop_ingress_admissions WHERE action IN ('reminder', 'reminder_email')")
    op.drop_constraint(op.f("ck_shop_ingress_admissions_action"), "shop_ingress_admissions", type_="check")
    op.create_check_constraint(op.f("ck_shop_ingress_admissions_action"), "shop_ingress_admissions",
        "action IN ('otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart')")
