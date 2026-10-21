"""Extend shared Shop admission buckets to all storefront input endpoints."""
from alembic import op

revision = '0139_shop_input_guards'
down_revision = '0138_shop_content_en'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f('ck_shop_ingress_admissions_action'), 'shop_ingress_admissions', type_='check')
    op.create_check_constraint(op.f('ck_shop_ingress_admissions_action'), 'shop_ingress_admissions',
                               "action IN ('otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart')")


def downgrade():
    # These are admission counters, not business records.
    op.execute("DELETE FROM shop_ingress_admissions WHERE action NOT IN ('otp_send', 'otp_verify', 'checkout')")
    op.drop_constraint(op.f('ck_shop_ingress_admissions_action'), 'shop_ingress_admissions', type_='check')
    op.create_check_constraint(op.f('ck_shop_ingress_admissions_action'), 'shop_ingress_admissions',
                               "action IN ('otp_send', 'otp_verify', 'checkout')")
