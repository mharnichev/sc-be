"""Mobile inventory permissions and product provenance/retry identity."""
from alembic import op
import sqlalchemy as sa

revision = "0107_mobile_inventory"
down_revision = "0106_booking_sms_consent"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("admin_users", sa.Column("can_post_inventory", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Existing non-master backoffice administrators retain inventory posting.
    op.execute("UPDATE admin_users SET can_post_inventory = true WHERE NOT EXISTS "
               "(SELECT 1 FROM masters WHERE masters.admin_user_id = admin_users.id)")
    for column in ("created_by_user_id", "barcode_bound_by_user_id"):
        op.add_column("products", sa.Column(column, sa.Integer(), nullable=True))
        op.create_foreign_key(f"fk_products_{column}_admin_users", "products", "admin_users", [column], ["id"], ondelete="SET NULL")
    op.add_column("products", sa.Column("creation_idempotency_key", sa.String(128), nullable=True))
    op.add_column("products", sa.Column("creation_payload", sa.JSON(), nullable=True))
    op.create_unique_constraint("uq_products_creation_idempotency_key", "products", ["creation_idempotency_key"])


def downgrade():
    op.drop_constraint("uq_products_creation_idempotency_key", "products", type_="unique")
    op.drop_column("products", "creation_payload")
    op.drop_column("products", "creation_idempotency_key")
    for column in ("barcode_bound_by_user_id", "created_by_user_id"):
        op.drop_constraint(f"fk_products_{column}_admin_users", "products", type_="foreignkey")
        op.drop_column("products", column)
    op.drop_column("admin_users", "can_post_inventory")
