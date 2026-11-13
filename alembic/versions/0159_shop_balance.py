"""Create the shop's manual income and expense journal."""

from alembic import op
import sqlalchemy as sa

revision = "0159_shop_balance"
down_revision = "0158_shop_alert_intents"
branch_labels = None
depends_on = None


INGRESS_ACTIONS = "('otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart', 'reminder', 'reminder_email')"


def set_ingress_actions(actions: str) -> None:
    name = op.f("ck_shop_ingress_admissions_action")
    op.drop_constraint(name, "shop_ingress_admissions", type_="check")
    op.create_check_constraint(name, "shop_ingress_admissions", f"action IN {actions}")


def upgrade() -> None:
    set_ingress_actions(INGRESS_ACTIONS[:-1] + ", 'balance_login')")
    op.create_table(
        "shop_balance_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(7), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("author_email", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('income', 'expense')", name="valid_kind"),
        sa.CheckConstraint("amount_minor BETWEEN 1 AND 999999999999", name="valid_amount"),
    )
    op.create_index("ix_shop_balance_entries_created_at", "shop_balance_entries", ["created_at"])


def downgrade() -> None:
    op.drop_table("shop_balance_entries")
    op.execute("DELETE FROM shop_ingress_admissions WHERE action = 'balance_login'")
    set_ingress_actions(INGRESS_ACTIONS)
