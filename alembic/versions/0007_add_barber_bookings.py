"""add barber bookings

Revision ID: 0007_barber_bookings
Revises: 0006_orders_customers
Create Date: 2026-05-09 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from textwrap import dedent


revision = "0007_barber_bookings"
down_revision = "0006_orders_customers"
branch_labels = None
depends_on = None


booking_status = pg.ENUM(
    "pending",
    "confirmed",
    "cancelled",
    "completed",
    name="bookingstatus",
    create_type=False,
)


def _create_enum_type(enum_type: pg.ENUM) -> None:
    values_sql = ", ".join(f"'{value}'" for value in enum_type.enums)
    op.execute(
        sa.text(
            dedent(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{enum_type.name}') THEN
                        CREATE TYPE {enum_type.name} AS ENUM ({values_sql});
                    END IF;
                END
                $$;
                """
            )
        )
    )


def _drop_enum_type(enum_type: sa.Enum) -> None:
    op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_type.name} CASCADE;"))


def _archive_legacy_tables() -> None:
    op.execute(
        sa.text(
            dedent(
                """
                DO $$
                BEGIN
                    IF to_regclass('public.bookings') IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                                AND table_name = 'bookings'
                                AND column_name = 'master_id'
                        )
                        AND to_regclass('public.legacy_bookings') IS NULL
                    THEN
                        ALTER TABLE bookings RENAME CONSTRAINT pk_bookings TO pk_legacy_bookings;
                        ALTER TABLE bookings RENAME TO legacy_bookings;
                    END IF;

                    IF to_regclass('public.barbers') IS NOT NULL
                        AND to_regclass('public.legacy_barbers') IS NULL
                    THEN
                        ALTER TABLE barbers RENAME CONSTRAINT pk_barbers TO pk_legacy_barbers;
                        ALTER TABLE barbers RENAME CONSTRAINT uq_barbers_slug TO uq_legacy_barbers_slug;
                        ALTER TABLE barbers RENAME TO legacy_barbers;
                        ALTER INDEX IF EXISTS ix_barbers_slug RENAME TO ix_legacy_barbers_slug;
                    END IF;

                    IF to_regclass('public.services') IS NOT NULL
                        AND to_regclass('public.legacy_services') IS NULL
                    THEN
                        ALTER TABLE services RENAME CONSTRAINT pk_services TO pk_legacy_services;
                        ALTER TABLE services RENAME CONSTRAINT uq_services_slug TO uq_legacy_services_slug;
                        ALTER TABLE services RENAME TO legacy_services;
                        ALTER INDEX IF EXISTS ix_services_slug RENAME TO ix_legacy_services_slug;
                    END IF;
                END
                $$;
                """
            )
        )
    )


def upgrade() -> None:
    _create_enum_type(booking_status)
    _archive_legacy_tables()

    op.create_table(
        "booking_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "masters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_user_id", sa.Integer(), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("photo_url", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("admin_user_id"),
    )

    op.create_table(
        "master_services",
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["booking_services.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("master_id", "service_id"),
    )

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=False),
        sa.Column("customer_phone", sa.String(length=50), nullable=False),
        sa.Column("customer_comment", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", booking_status, nullable=False, server_default="confirmed"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["booking_services.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bookings_master_id", "bookings", ["master_id"])
    op.create_index("ix_bookings_service_id", "bookings", ["service_id"])
    op.create_index("ix_bookings_start_at", "bookings", ["start_at"])
    op.create_index("ix_bookings_end_at", "bookings", ["end_at"])
    op.create_index("ix_bookings_status", "bookings", ["status"])

    op.create_table(
        "master_time_blocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_master_time_blocks_master_id", "master_time_blocks", ["master_id"])
    op.create_index("ix_master_time_blocks_start_at", "master_time_blocks", ["start_at"])
    op.create_index("ix_master_time_blocks_end_at", "master_time_blocks", ["end_at"])


def downgrade() -> None:
    op.drop_index("ix_master_time_blocks_end_at", table_name="master_time_blocks")
    op.drop_index("ix_master_time_blocks_start_at", table_name="master_time_blocks")
    op.drop_index("ix_master_time_blocks_master_id", table_name="master_time_blocks")
    op.drop_table("master_time_blocks")
    op.drop_index("ix_bookings_status", table_name="bookings")
    op.drop_index("ix_bookings_end_at", table_name="bookings")
    op.drop_index("ix_bookings_start_at", table_name="bookings")
    op.drop_index("ix_bookings_service_id", table_name="bookings")
    op.drop_index("ix_bookings_master_id", table_name="bookings")
    op.drop_table("bookings")
    op.drop_table("master_services")
    op.drop_table("masters")
    op.drop_table("booking_services")
    _drop_enum_type(booking_status)
