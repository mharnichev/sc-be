"""Record the last Shop balance edit without rewriting creation details."""

from alembic import op
import sqlalchemy as sa

revision = "0162_shop_balance_edits"
down_revision = "0161_shop_mono_expired"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shop_balance_entries", sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.add_column("shop_balance_entries", sa.Column("updated_by_email", sa.String(255)))


def downgrade() -> None:
    op.drop_column("shop_balance_entries", "updated_by_email")
    op.drop_column("shop_balance_entries", "updated_at")
