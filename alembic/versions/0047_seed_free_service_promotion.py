"""seed free service promotion

Revision ID: 0047_free_service_promotion
Revises: 0046_product_volume_variants
Create Date: 2026-07-26 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0047_free_service_promotion"
down_revision = "0046_product_volume_variants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "promotions",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
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
                is_public,
                is_active
            )
            VALUES (
                'FREE100',
                'Безкоштовна послуга',
                'Free service',
                'Промокод для безкоштовного надання вибраних у записі послуг.',
                'Promo code for providing the services selected in a booking free of charge.',
                'percent'::promotiondiscounttype,
                100,
                'all_customers'::promotioneligibilitytype,
                NULL,
                true,
                true,
                false,
                true
            )
            ON CONFLICT (code) DO UPDATE
            SET
                name_uk = EXCLUDED.name_uk,
                name_en = EXCLUDED.name_en,
                description_uk = EXCLUDED.description_uk,
                description_en = EXCLUDED.description_en,
                discount_type = EXCLUDED.discount_type,
                discount_percent = EXCLUDED.discount_percent,
                eligibility_type = EXCLUDED.eligibility_type,
                inactive_days = EXCLUDED.inactive_days,
                applies_to_all_masters = EXCLUDED.applies_to_all_masters,
                applies_to_all_services = EXCLUDED.applies_to_all_services,
                is_public = EXCLUDED.is_public,
                is_active = EXCLUDED.is_active
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM promotions WHERE code = 'FREE100'"))
    op.drop_column("promotions", "is_public")
