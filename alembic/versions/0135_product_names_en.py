"""Optional English product names without changing imported source values."""
from alembic import op
import sqlalchemy as sa

revision = "0135_product_names_en"
down_revision = "0134_shop_canonical_collation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("name_en", sa.String(255), nullable=True))
    op.add_column("products", sa.Column("product_type_en", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "product_type_en")
    op.drop_column("products", "name_en")
