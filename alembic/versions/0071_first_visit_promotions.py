"""Add automatic first-visit promotions and durable entitlement tracking.

Revision ID: 0071_first_visit_promotions
Revises: 0070_product_ingredients
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0071_first_visit_promotions"
down_revision = "0070_product_ingredients"
branch_labels = None
depends_on = None

application_mode = postgresql.ENUM(
    "code", "automatic", name="promotionapplicationmode", create_type=False,
)


def upgrade() -> None:
    # PostgreSQL requires an enum value to be committed before it can be used.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE promotioneligibilitytype ADD VALUE IF NOT EXISTS 'first_visit'")
        op.execute("ALTER TYPE bookingstatus ADD VALUE IF NOT EXISTS 'no_show'")
    application_mode.create(op.get_bind(), checkfirst=True)
    op.add_column("promotions", sa.Column(
        "application_mode", application_mode, nullable=False, server_default="code",
    ))
    op.add_column("customers", sa.Column("first_visit_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("first_visit_customer_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_bookings_first_visit_customer_id_customers"), "bookings", "customers",
        ["first_visit_customer_id"], ["id"], ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_bookings_first_visit_customer_id"), "bookings", ["first_visit_customer_id"],
    )
    op.add_column("bookings", sa.Column("promotion_application_mode_snapshot", sa.String(20), nullable=True))
    op.add_column("bookings", sa.Column("promotion_eligibility_type_snapshot", sa.String(30), nullable=True))
    op.execute(sa.text("""
        UPDATE customers AS c
        SET first_visit_completed_at = visits.first_visit
        FROM (
            SELECT customer_id, MIN(COALESCE(completed_at, end_at)) AS first_visit
            FROM bookings WHERE status = 'completed' AND customer_id IS NOT NULL
            GROUP BY customer_id
        ) AS visits
        WHERE c.id = visits.customer_id AND c.first_visit_completed_at IS NULL
    """))
    op.execute(sa.text("""
        UPDATE customers
        SET first_visit_completed_at = imported_last_visit_at
        WHERE first_visit_completed_at IS NULL AND imported_last_visit_at IS NOT NULL
    """))
    op.execute(sa.text("""
        UPDATE bookings AS b
        SET promotion_application_mode_snapshot = 'code',
            promotion_eligibility_type_snapshot = p.eligibility_type::text
        FROM promotions AS p
        WHERE b.promotion_id = p.id
    """))
    op.execute(sa.text("""
        INSERT INTO promotions (
            code, name_uk, name_en, description_uk, description_en,
            discount_type, discount_percent, eligibility_type, application_mode,
            applies_to_all_masters, applies_to_all_services, is_public, is_active,
            created_at, updated_at
        ) VALUES (
            'FIRST_VISIT20', 'Перший візит', 'First visit',
            'Знижка на перший завершений візит до барбершопу, незалежно від майстра. Без промокоду.',
            'Discount on your first completed visit to the barbershop, across all masters. No code required.',
            'percent', 20, 'first_visit', 'automatic', true, true, true, true, now(), now()
        ) ON CONFLICT (code) DO NOTHING
    """))


def downgrade() -> None:
    op.execute("UPDATE bookings SET status = 'cancelled' WHERE status = 'no_show'")
    # Preserve administrator edits and references; deactivate the unsupported mode.
    op.execute("UPDATE promotions SET is_active = false WHERE application_mode = 'automatic' OR eligibility_type = 'first_visit'")
    op.execute("UPDATE promotions SET eligibility_type = 'all_customers' WHERE eligibility_type = 'first_visit'")
    op.drop_column("bookings", "promotion_eligibility_type_snapshot")
    op.drop_column("bookings", "promotion_application_mode_snapshot")
    op.drop_constraint(op.f("uq_bookings_first_visit_customer_id"), "bookings", type_="unique")
    op.drop_constraint(op.f("fk_bookings_first_visit_customer_id_customers"), "bookings", type_="foreignkey")
    op.drop_column("bookings", "first_visit_customer_id")
    op.drop_column("customers", "first_visit_completed_at")
    op.drop_column("promotions", "application_mode")
    application_mode.drop(op.get_bind(), checkfirst=True)
    # Enum labels cannot be removed safely while older data/code may reference them.
