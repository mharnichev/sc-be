"""Optional English shop content without changing source or order snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0138_shop_content_en"
down_revision = "0137_shop_online_payment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("description_en", "short_description_en", "ingredients_en"):
        op.add_column("products", sa.Column(name, sa.Text(), nullable=True))
    op.add_column("categories", sa.Column("description_en", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("categories", "description_en")
    for name in ("ingredients_en", "short_description_en", "description_en"):
        op.drop_column("products", name)
