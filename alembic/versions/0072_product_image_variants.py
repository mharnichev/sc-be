"""Persist immutable generated product-image variants.

Revision ID: 0072_product_image_variants
Revises: 0071_first_visit_promotions
"""

from alembic import op
import sqlalchemy as sa


revision = "0072_product_image_variants"
down_revision = "0071_first_visit_promotions"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "product_image_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("source_image_id", sa.Integer(), nullable=True),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("preset", sa.String(length=64), nullable=False),
        sa.Column("recipe_version", sa.String(length=64), nullable=False),
        sa.Column("processor_version", sa.String(length=128), nullable=True),
        sa.Column("processing_config", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_upload_id", sa.Integer(), nullable=True),
        sa.Column("output_url", sa.String(length=500), nullable=True),
        sa.Column("is_preferred", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.CheckConstraint("status IN ('pending', 'processing', 'succeeded', 'failed')", name="product_image_variant_status"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_image_id"], ["product_images.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["output_upload_id"], ["uploads.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "product_id", "source_key", "source_fingerprint", "preset", "recipe_version",
            name="uq_product_image_variants_source_recipe",
        ),
    )
    op.create_index("ix_product_image_variants_product_id", "product_image_variants", ["product_id"])
    op.create_index("ix_product_image_variants_source_image_id", "product_image_variants", ["source_image_id"])
    op.create_index(
        "ix_product_image_variants_preferred",
        "product_image_variants",
        ["product_id", "status", "is_preferred"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_image_variants_preferred", table_name="product_image_variants")
    op.drop_index("ix_product_image_variants_source_image_id", table_name="product_image_variants")
    op.drop_index("ix_product_image_variants_product_id", table_name="product_image_variants")
    op.drop_table("product_image_variants")
