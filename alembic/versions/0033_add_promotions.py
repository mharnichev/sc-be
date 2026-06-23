"""add promotions

Revision ID: 0033_add_promotions
Revises: 0032_sms_scenarios
Create Date: 2026-06-23 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0033_add_promotions"
down_revision = "0032_sms_scenarios"
branch_labels = None
depends_on = None


promotion_discount_type = postgresql.ENUM(
    "percent",
    name="promotiondiscounttype",
    create_type=False,
)
promotion_eligibility_type = postgresql.ENUM(
    "all_customers",
    "inactive_customers",
    name="promotioneligibilitytype",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    promotion_discount_type.create(bind, checkfirst=True)
    promotion_eligibility_type.create(bind, checkfirst=True)

    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name_uk", sa.String(length=255), nullable=False),
        sa.Column("name_en", sa.String(length=255), nullable=False),
        sa.Column("description_uk", sa.Text(), nullable=True),
        sa.Column("description_en", sa.Text(), nullable=True),
        sa.Column("discount_type", promotion_discount_type, nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False),
        sa.Column("eligibility_type", promotion_eligibility_type, nullable=False),
        sa.Column("inactive_days", sa.Integer(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promotions")),
    )
    op.create_index("ix_promotions_code", "promotions", ["code"], unique=True)
    op.create_index("ix_promotions_is_active", "promotions", ["is_active"], unique=False)

    op.add_column("bookings", sa.Column("promotion_id", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("promotion_code_snapshot", sa.String(length=50), nullable=True))
    op.add_column("bookings", sa.Column("promotion_name_uk_snapshot", sa.String(length=255), nullable=True))
    op.add_column("bookings", sa.Column("promotion_name_en_snapshot", sa.String(length=255), nullable=True))
    op.add_column("bookings", sa.Column("promotion_discount_percent_snapshot", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("subtotal_amount", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("promotion_discount_amount", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("total_amount", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_bookings_promotion_id"), "bookings", ["promotion_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_bookings_promotion_id_promotions"),
        "bookings",
        "promotions",
        ["promotion_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute(
        sa.text(
            """
            UPDATE bookings
            SET subtotal_amount = service_totals.subtotal_amount,
                promotion_discount_amount = 0,
                total_amount = service_totals.subtotal_amount
            FROM (
                SELECT
                    bookings.id AS booking_id,
                    COALESCE(SUM(barber_services.price), 0)::integer AS subtotal_amount
                FROM bookings
                LEFT JOIN booking_service_items ON booking_service_items.booking_id = bookings.id
                LEFT JOIN barber_services
                    ON barber_services.id = COALESCE(booking_service_items.service_id, bookings.service_id)
                GROUP BY bookings.id
            ) AS service_totals
            WHERE bookings.id = service_totals.booking_id
            """
        )
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
                is_active
            )
            VALUES (
                'COMEBACK15',
                'Повернення клієнта',
                'Comeback client',
                'Знижка 15% для клієнтів, які не відвідували барбершоп останні 90 днів.',
                '15% discount for clients who have not visited the barbershop in the last 90 days.',
                'percent'::promotiondiscounttype,
                15,
                'inactive_customers'::promotioneligibilitytype,
                90,
                true
            )
            ON CONFLICT (code) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_bookings_promotion_id_promotions"), "bookings", type_="foreignkey")
    op.drop_index(op.f("ix_bookings_promotion_id"), table_name="bookings")
    op.drop_column("bookings", "total_amount")
    op.drop_column("bookings", "promotion_discount_amount")
    op.drop_column("bookings", "subtotal_amount")
    op.drop_column("bookings", "promotion_discount_percent_snapshot")
    op.drop_column("bookings", "promotion_name_en_snapshot")
    op.drop_column("bookings", "promotion_name_uk_snapshot")
    op.drop_column("bookings", "promotion_code_snapshot")
    op.drop_column("bookings", "promotion_id")

    op.drop_index("ix_promotions_is_active", table_name="promotions")
    op.drop_index("ix_promotions_code", table_name="promotions")
    op.drop_table("promotions")

    promotion_eligibility_type.drop(op.get_bind(), checkfirst=True)
    promotion_discount_type.drop(op.get_bind(), checkfirst=True)
