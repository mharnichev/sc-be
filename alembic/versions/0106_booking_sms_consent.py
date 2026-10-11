"""Audit service SMS consent accepted with a public booking."""
from alembic import op
import sqlalchemy as sa

revision = "0106_booking_sms_consent"
down_revision = "0105_customer_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("client_communication_preferences", sa.Column(
        "booking_sms_consent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("client_communication_preferences", sa.Column(
        "booking_sms_consent_booking_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_preferences_booking_sms_consent", "client_communication_preferences",
                          "bookings", ["booking_sms_consent_booking_id"], ["id"], ondelete="SET NULL")


def downgrade():
    op.drop_constraint("fk_preferences_booking_sms_consent", "client_communication_preferences", type_="foreignkey")
    op.drop_column("client_communication_preferences", "booking_sms_consent_booking_id")
    op.drop_column("client_communication_preferences", "booking_sms_consent_at")
