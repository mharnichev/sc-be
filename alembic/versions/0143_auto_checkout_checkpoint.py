"""Automatic one-shot reminder checkpoint facts and monotonic event fencing.

Rename the previous development-only fields without enrolling historical carts.
No per-user consent or mailbox verification gate is added.
"""
from alembic import op
import sqlalchemy as sa

revision = "0143_auto_checkout_checkpoint"
down_revision = "0142_checkout_reminders"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("checkout_reminders", "consent_at", new_column_name="checkpoint_at")
    op.alter_column("checkout_reminders", "consent_version", new_column_name="checkpoint_version")
    op.add_column("checkout_reminders", sa.Column("checkpoint_revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint(op.f("ck_checkout_reminders_checkout_reminder_revision"), "checkout_reminders",
        "checkpoint_revision >= 0 AND checkpoint_revision <= 2147483647")


def downgrade():
    op.drop_constraint(op.f("ck_checkout_reminders_checkout_reminder_revision"), "checkout_reminders", type_="check")
    op.drop_column("checkout_reminders", "checkpoint_revision")
    op.alter_column("checkout_reminders", "checkpoint_at", new_column_name="consent_at")
    op.alter_column("checkout_reminders", "checkpoint_version", new_column_name="consent_version")
