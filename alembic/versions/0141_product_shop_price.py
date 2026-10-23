"""Initialize independent shop prices from RRP, falling back to current price.

Human-approved policy: existing storefront prices intentionally change to RRP
where present. Supplier price and RRP columns remain unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = "0141_product_shop_price"
down_revision = "0140_shop_order_receipt_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("shop_price", sa.Numeric(10, 2), nullable=True))
    # One-time approved initialization; subsequent imports preserve shop_price.
    op.execute("UPDATE products SET shop_price = COALESCE(recommended_retail_price, price)")
    op.create_check_constraint("products_shop_price_nonnegative", "products", "shop_price >= 0")


def downgrade():
    op.drop_constraint("products_shop_price_nonnegative", "products", type_="check")
    op.drop_column("products", "shop_price")
