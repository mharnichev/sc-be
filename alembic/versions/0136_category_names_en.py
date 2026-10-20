"""Optional English category names, preserving source names and hierarchy."""
from alembic import op
import sqlalchemy as sa

revision = "0136_category_names_en"
down_revision = "0135_product_names_en"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("name_en", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("categories", "name_en")
