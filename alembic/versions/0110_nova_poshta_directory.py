"""Searchable Nova Poshta directory; existing order JSON stores provider references."""
from alembic import op
import sqlalchemy as sa

revision = "0110_nova_poshta_directory"
down_revision = "0109_telegram_booking_funnel"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nova_poshta_settlements",
        sa.Column("ref", sa.String(36), primary_key=True),
        sa.Column("city_ref", sa.String(36), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("popular_rank", sa.Integer(), nullable=False),
        sa.Column("delivery_available", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "nova_poshta_branches",
        sa.Column("ref", sa.String(36), primary_key=True),
        sa.Column("settlement_ref", sa.String(36), nullable=False),
        sa.Column("number", sa.String(32), nullable=False),
        sa.Column("sort_number", sa.Integer(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_nova_poshta_branches_settlement_ref", "nova_poshta_branches", ["settlement_ref"])


def downgrade():
    op.drop_table("nova_poshta_branches")
    op.drop_table("nova_poshta_settlements")
