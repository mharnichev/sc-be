"""Keep callback preference and item presentation as checkout order facts.

Historical orders stay NULL; today's catalog cannot reconstruct their receipt.
"""
from alembic import op
import sqlalchemy as sa

revision = "0140_shop_order_receipt_snapshot"
down_revision = "0139_shop_input_guards"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("callback_requested", sa.Boolean(), nullable=True))
    op.add_column("order_items", sa.Column("product_image_url", sa.String(1000), nullable=True))
    op.add_column("order_items", sa.Column("product_variant", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("order_items", "product_variant")
    op.drop_column("order_items", "product_image_url")
    op.drop_column("orders", "callback_requested")
