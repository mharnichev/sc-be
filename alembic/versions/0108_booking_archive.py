"""Retain deleted bookings while excluding them from operational reads."""
from alembic import op
import sqlalchemy as sa

revision = "0108_booking_archive"
down_revision = "0107_mobile_inventory"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bookings", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_bookings_archived_at", "bookings", ["archived_at"])


def downgrade():
    op.drop_index("ix_bookings_archived_at", table_name="bookings")
    op.drop_column("bookings", "archived_at")
