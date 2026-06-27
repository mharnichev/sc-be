"""add promotion scopes

Revision ID: 0034_add_promotion_scopes
Revises: 0033_add_promotions
Create Date: 2026-06-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0034_add_promotion_scopes"
down_revision = "0033_add_promotions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE promotioneligibilitytype ADD VALUE IF NOT EXISTS 'military_customers'")

    op.add_column(
        "promotions",
        sa.Column("applies_to_all_masters", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "promotions",
        sa.Column("applies_to_all_services", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("promotions", "applies_to_all_masters", server_default=None)
    op.alter_column("promotions", "applies_to_all_services", server_default=None)

    op.create_table(
        "promotion_masters",
        sa.Column("promotion_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["master_id"],
            ["masters.id"],
            name=op.f("fk_promotion_masters_master_id_masters"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["promotion_id"],
            ["promotions.id"],
            name=op.f("fk_promotion_masters_promotion_id_promotions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("promotion_id", "master_id", name=op.f("pk_promotion_masters")),
    )
    op.create_table(
        "promotion_base_services",
        sa.Column("promotion_id", sa.Integer(), nullable=False),
        sa.Column("base_service_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["base_service_id"],
            ["base_services.id"],
            name=op.f("fk_promotion_base_services_base_service_id_base_services"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["promotion_id"],
            ["promotions.id"],
            name=op.f("fk_promotion_base_services_promotion_id_promotions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("promotion_id", "base_service_id", name=op.f("pk_promotion_base_services")),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO promotions (
                code,
                name_uk,
                name_en,
                description_uk,
                description_en,
                discount_type,
                discount_percent,
                eligibility_type,
                inactive_days,
                applies_to_all_masters,
                applies_to_all_services,
                is_active
            )
            SELECT
                'ZSU50',
                'Знижка для захисників',
                'Defender discount',
                'Знижка 50% для військових на вибрані послуги у вибраних майстрів.',
                '50% discount for military clients on selected services with selected masters.',
                'percent'::promotiondiscounttype,
                50,
                'military_customers'::promotioneligibilitytype,
                NULL,
                false,
                false,
                true
            WHERE EXISTS (
                SELECT 1
                FROM barber_services
                WHERE is_army_client IS TRUE
            )
            OR EXISTS (
                SELECT 1
                FROM base_services
                WHERE is_army_client IS TRUE
            )
            ON CONFLICT (code) DO UPDATE
            SET
                discount_type = EXCLUDED.discount_type,
                discount_percent = EXCLUDED.discount_percent,
                eligibility_type = EXCLUDED.eligibility_type,
                inactive_days = EXCLUDED.inactive_days,
                applies_to_all_masters = false,
                applies_to_all_services = false,
                is_active = true
            """
        )
    )

    op.execute(
        sa.text(
            """
            WITH zsu AS (
                SELECT id FROM promotions WHERE code = 'ZSU50'
            ),
            scoped_base_services AS (
                SELECT id AS base_service_id
                FROM base_services
                WHERE is_army_client IS TRUE
                UNION
                SELECT base_service_id
                FROM barber_services
                WHERE is_army_client IS TRUE
                    AND base_service_id IS NOT NULL
            )
            INSERT INTO promotion_base_services (promotion_id, base_service_id)
            SELECT DISTINCT zsu.id, scoped_base_services.base_service_id
            FROM zsu
            CROSS JOIN scoped_base_services
            ON CONFLICT DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            """
            WITH zsu AS (
                SELECT id FROM promotions WHERE code = 'ZSU50'
            ),
            scoped_base_services AS (
                SELECT base_service_id
                FROM promotion_base_services
                JOIN zsu ON zsu.id = promotion_base_services.promotion_id
            )
            INSERT INTO promotion_masters (promotion_id, master_id)
            SELECT DISTINCT zsu.id, barber_services.master_id
            FROM zsu
            JOIN barber_services ON barber_services.base_service_id IN (
                SELECT base_service_id FROM scoped_base_services
            )
            ON CONFLICT DO NOTHING
            """
        )
    )

    op.drop_column("barber_services", "is_army_client")
    op.drop_column("base_services", "is_army_client")


def downgrade() -> None:
    op.add_column(
        "base_services",
        sa.Column("is_army_client", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "barber_services",
        sa.Column("is_army_client", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("base_services", "is_army_client", server_default=None)
    op.alter_column("barber_services", "is_army_client", server_default=None)

    op.drop_table("promotion_base_services")
    op.drop_table("promotion_masters")
    op.drop_column("promotions", "applies_to_all_services")
    op.drop_column("promotions", "applies_to_all_masters")
