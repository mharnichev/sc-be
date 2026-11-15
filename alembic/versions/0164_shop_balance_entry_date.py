"""Add the date used to account for a balance entry."""

import sqlalchemy as sa
from alembic import op

revision = "0164_shop_balance_entry_date"
down_revision = "0163_master_mono_refunds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shop_balance_entries", sa.Column("entry_date", sa.Date()))


def downgrade() -> None:
    op.drop_column("shop_balance_entries", "entry_date")
