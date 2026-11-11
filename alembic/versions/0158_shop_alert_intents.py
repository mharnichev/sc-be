"""Persist deduplicated local Shop operations alert intents."""
from alembic import op
import sqlalchemy as sa

revision = "0158_shop_alert_intents"
down_revision = "0157_direct_carrier_refunds"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shop_operations_alert_intents",
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("reason", "entity_id", "since"),
    )


def downgrade():
    op.drop_table("shop_operations_alert_intents")
