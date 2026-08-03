"""add booking service prices

Revision ID: 0054_booking_service_prices
Revises: 0053_manual_booking_discounts
Create Date: 2026-08-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0054_booking_service_prices"
down_revision = "0053_manual_booking_discounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_service_items", sa.Column("price_amount", sa.Integer(), nullable=True))
    op.execute(
        """
        WITH source_prices AS (
            SELECT
                booking_service_items.id,
                booking_service_items.booking_id,
                booking_service_items.position,
                COALESCE(barber_services.price, 0)::integer AS catalog_price,
                COALESCE(
                    bookings.subtotal_amount,
                    SUM(COALESCE(barber_services.price, 0)) OVER (
                        PARTITION BY booking_service_items.booking_id
                    )
                )::integer AS target_total,
                SUM(COALESCE(barber_services.price, 0)) OVER (
                    PARTITION BY booking_service_items.booking_id
                )::integer AS catalog_total,
                ROW_NUMBER() OVER (
                    PARTITION BY booking_service_items.booking_id
                    ORDER BY booking_service_items.position, booking_service_items.id
                ) AS item_number,
                COUNT(*) OVER (
                    PARTITION BY booking_service_items.booking_id
                ) AS item_count
            FROM booking_service_items
            JOIN bookings ON bookings.id = booking_service_items.booking_id
            JOIN barber_services ON barber_services.id = booking_service_items.service_id
        ),
        allocated_prices AS (
            SELECT
                source_prices.*,
                CASE
                    WHEN item_count = 1 THEN target_total
                    WHEN catalog_total > 0 THEN FLOOR(
                        target_total::numeric * catalog_price::numeric / catalog_total::numeric
                    )::integer
                    ELSE FLOOR(target_total::numeric / item_count::numeric)::integer
                END AS allocated_price
            FROM source_prices
        ),
        balanced_prices AS (
            SELECT
                allocated_prices.*,
                SUM(allocated_price) OVER (PARTITION BY booking_id)::integer AS allocated_total
            FROM allocated_prices
        )
        UPDATE booking_service_items
        SET price_amount = CASE
            WHEN balanced_prices.item_number = balanced_prices.item_count
                THEN balanced_prices.target_total
                    - (balanced_prices.allocated_total - balanced_prices.allocated_price)
            ELSE balanced_prices.allocated_price
        END
        FROM balanced_prices
        WHERE balanced_prices.id = booking_service_items.id
        """
    )
    op.alter_column("booking_service_items", "price_amount", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    op.drop_column("booking_service_items", "price_amount")
