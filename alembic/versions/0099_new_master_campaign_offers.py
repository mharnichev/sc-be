"""typed campaign settings and recipient-bound offer entitlements

Revision ID: 0099_campaign_offers
Revises: 0098_recovery_analytics
"""
from alembic import op
import sqlalchemy as sa

revision = "0099_campaign_offers"
down_revision = "0098_recovery_analytics"
branch_labels = None
depends_on = None


def upgrade():
    for name, target in (("offer_master_id", "masters"), ("offer_promotion_id", "promotions")):
        op.add_column("campaigns", sa.Column(name, sa.Integer(), nullable=True))
        op.create_foreign_key(None, "campaigns", target, [name], ["id"], ondelete="RESTRICT")
    for name in ("offer_starts_at", "offer_expires_at"):
        op.add_column("campaigns", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    for name in ("offer_service_ids", "sending_window"):
        op.add_column("campaigns", sa.Column(name, sa.JSON(), nullable=True))
    op.add_column("campaigns", sa.Column("master_name_for_message", sa.String(255), nullable=True))
    for name in ("sms_recipients_per_minute", "marketing_frequency_days", "marketing_max_contacts", "marketing_cap_days"):
        op.add_column("campaigns", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column("promotions", sa.Column("recipient_offer_only", sa.Boolean(), nullable=False, server_default=sa.false()))
    offer_columns = [sa.Column("id", sa.Integer(), primary_key=True)]
    for name, target in (("campaign_id", "campaigns"), ("run_id", "campaign_runs"), ("recipient_id", "message_recipients"),
                         ("customer_id", "customers"), ("master_id", "masters"), ("promotion_id", "promotions")):
        offer_columns.append(sa.Column(name, sa.Integer(), sa.ForeignKey(target+".id", ondelete="RESTRICT"), nullable=False,
                                       unique=name == "recipient_id"))
    offer_columns += [sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("allowed_service_ids", sa.JSON(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False),
        sa.Column("promotion_name_uk", sa.String(255), nullable=False), sa.Column("promotion_name_en", sa.String(255), nullable=False),
        sa.Column("conditions_snapshot", sa.JSON(), nullable=False),
        sa.Column("consumed_booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=True, unique=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_discount_amount", sa.Integer(), nullable=True),
        sa.Column("redeemed_revenue_amount", sa.Integer(), nullable=True),
        sa.Column("raw_link_requests", sa.Integer(), nullable=False), sa.Column("confirmed_open_count", sa.Integer(), nullable=False),
        sa.Column("first_confirmed_open_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]
    op.create_table("campaign_offers", *offer_columns)
    for name in ("campaign_id", "run_id", "customer_id", "master_id", "promotion_id", "expires_at"):
        op.create_index("ix_campaign_offers_"+name, "campaign_offers", [name])
    op.create_table("campaign_offer_opens", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("offer_id", sa.Integer(), sa.ForeignKey("campaign_offers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("offer_id", "event_hash", name="uq_campaign_offer_open_event"))
    op.create_index("ix_campaign_offer_opens_offer_id", "campaign_offer_opens", ["offer_id"])
    for name, target in (("campaign_offer_id", "campaign_offers"), ("campaign_id", "campaigns"),
                         ("campaign_run_id", "campaign_runs"), ("campaign_recipient_id", "message_recipients"),
                         ("campaign_offer_reservation_id", "campaign_offers")):
        op.add_column("bookings", sa.Column(name, sa.Integer(), nullable=True))
        op.create_foreign_key(None, "bookings", target, [name], ["id"], ondelete="RESTRICT")
        if name == "campaign_offer_reservation_id":
            op.create_unique_constraint("uq_bookings_campaign_offer_reservation_id", "bookings", [name])
        else:
            op.create_index("ix_bookings_"+name, "bookings", [name])


def downgrade():
    for name, target in (("campaign_offer_id", "campaign_offers"), ("campaign_id", "campaigns"),
                         ("campaign_run_id", "campaign_runs"), ("campaign_recipient_id", "message_recipients"),
                         ("campaign_offer_reservation_id", "campaign_offers")):
        if name == "campaign_offer_reservation_id":
            op.drop_constraint("uq_bookings_campaign_offer_reservation_id", "bookings", type_="unique")
        else:
            op.drop_index("ix_bookings_"+name, table_name="bookings")
        op.drop_constraint("fk_bookings_"+name+"_"+target, "bookings", type_="foreignkey")
        op.drop_column("bookings", name)
    op.drop_table("campaign_offer_opens")
    op.drop_table("campaign_offers")
    op.drop_column("promotions", "recipient_offer_only")
    for name, target in (("offer_master_id", "masters"), ("offer_promotion_id", "promotions")):
        op.drop_constraint("fk_campaigns_"+name+"_"+target, "campaigns", type_="foreignkey")
        op.drop_column("campaigns", name)
    for name in ("offer_starts_at", "offer_expires_at", "offer_service_ids", "sending_window", "master_name_for_message",
                 "sms_recipients_per_minute", "marketing_frequency_days", "marketing_max_contacts", "marketing_cap_days"):
        op.drop_column("campaigns", name)
