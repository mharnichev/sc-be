"""add base and barber services

Revision ID: 0009_base_barber_services
Revises: 0008_google_reviews_cache
Create Date: 2026-05-12 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_base_barber_services"
down_revision = "0008_google_reviews_cache"
branch_labels = None
depends_on = None


DEFAULT_BASE_SERVICES = (
    ("Традиційне гоління", 30, 800),
    ("Дитяча стрижка", 60, 900),
    ("Стрижка машинкою+стрижка бороди", 60, 1300),
    ("Стрижка+борода", 90, 1500),
    ("Стрижка бороди", 30, 600),
    ("Стрижка машинкою", 30, 700),
    ("Стрижка", 60, 900),
)


def upgrade() -> None:
    op.create_table(
        "base_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "barber_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("base_service_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("legacy_booking_service_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["base_service_id"], ["base_services.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_barber_services_master_id", "barber_services", ["master_id"])
    op.create_index("ix_barber_services_base_service_id", "barber_services", ["base_service_id"])
    op.create_index(
        "uq_barber_services_master_base_service",
        "barber_services",
        ["master_id", "base_service_id"],
        unique=True,
        postgresql_where=sa.text("base_service_id IS NOT NULL"),
    )
    op.create_index(
        "uq_barber_services_master_custom_name",
        "barber_services",
        ["master_id", "name"],
        unique=True,
        postgresql_where=sa.text("base_service_id IS NULL"),
    )

    for name, duration_minutes, price in DEFAULT_BASE_SERVICES:
        op.execute(
            sa.text(
                """
                INSERT INTO base_services (name, duration_minutes, price, is_active)
                VALUES (:name, :duration_minutes, :price, true)
                ON CONFLICT (name) DO NOTHING
                """
            ).bindparams(name=name, duration_minutes=duration_minutes, price=price)
        )

    op.execute(
        sa.text(
            """
            INSERT INTO barber_services (
                master_id,
                base_service_id,
                name,
                description,
                duration_minutes,
                price,
                is_active,
                legacy_booking_service_id
            )
            SELECT
                ms.master_id,
                base.id,
                bs.name,
                bs.description,
                bs.duration_minutes,
                bs.price::integer,
                bs.is_active,
                bs.id
            FROM booking_services bs
            JOIN master_services ms ON ms.service_id = bs.id
            LEFT JOIN base_services base ON base.name = bs.name
            ON CONFLICT DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO barber_services (
                master_id,
                base_service_id,
                name,
                description,
                duration_minutes,
                price,
                is_active
            )
            SELECT
                masters.id,
                base_services.id,
                base_services.name,
                base_services.description,
                base_services.duration_minutes,
                base_services.price,
                base_services.is_active
            FROM masters
            CROSS JOIN base_services
            WHERE base_services.is_active IS TRUE
            ON CONFLICT DO NOTHING
            """
        )
    )

    op.drop_constraint("fk_bookings_service_id_booking_services", "bookings", type_="foreignkey")
    op.execute(
        sa.text(
            """
            UPDATE bookings
            SET service_id = barber_services.id
            FROM barber_services
            WHERE barber_services.master_id = bookings.master_id
                AND barber_services.legacy_booking_service_id = bookings.service_id
            """
        )
    )
    op.create_foreign_key(
        "fk_bookings_service_id_barber_services",
        "bookings",
        "barber_services",
        ["service_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_column("barber_services", "legacy_booking_service_id")
    op.execute(sa.text("SELECT setval(pg_get_serial_sequence('barber_services', 'id'), COALESCE((SELECT max(id) FROM barber_services), 1))"))


def downgrade() -> None:
    op.drop_constraint("fk_bookings_service_id_barber_services", "bookings", type_="foreignkey")
    op.create_foreign_key(
        "fk_bookings_service_id_booking_services",
        "bookings",
        "booking_services",
        ["service_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_index("uq_barber_services_master_custom_name", table_name="barber_services")
    op.drop_index("uq_barber_services_master_base_service", table_name="barber_services")
    op.drop_index("ix_barber_services_base_service_id", table_name="barber_services")
    op.drop_index("ix_barber_services_master_id", table_name="barber_services")
    op.drop_table("barber_services")
    op.drop_table("base_services")
