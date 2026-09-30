"""add inventory workflow persistence

Revision ID: 0096_inventory_workflow
Revises: 0095_master_passport_photo
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0096_inventory_workflow"
down_revision = "0095_master_passport_photo"
branch_labels = None
depends_on = None


movement_type = postgresql.ENUM(
    "opening_balance", "receipt", "reservation", "reservation_release", "shipment",
    "customer_return", "write_off", "inventory_adjustment", name="inventory_movement_type",
    create_type=False,
)
document_status = postgresql.ENUM(
    "draft", "posted", name="inventory_document_status", create_type=False
)
procurement_status = postgresql.ENUM(
    "not_required", "to_order", "ordered", "received", name="procurement_status",
    create_type=False,
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    movement_type.create(bind, checkfirst=True)
    document_status.create(bind, checkfirst=True)
    procurement_status.create(bind, checkfirst=True)

    op.add_column("products", sa.Column("reserved_quantity", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("products", sa.Column("barcode", sa.String(length=64), nullable=True))
    op.add_column("products", sa.Column("allow_backorder", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_unique_constraint("uq_products_barcode", "products", ["barcode"])
    op.create_check_constraint("products_stock_quantity_nonnegative", "products", "stock_quantity >= 0")
    op.create_check_constraint("products_reserved_quantity_nonnegative", "products", "reserved_quantity >= 0")
    op.create_check_constraint("products_available_quantity_nonnegative", "products", "stock_quantity - reserved_quantity >= 0")

    op.add_column("order_items", sa.Column("quantity_from_stock", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("order_items", sa.Column("quantity_to_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("order_items", sa.Column("quantity_received_for_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "order_items",
        sa.Column("procurement_status", procurement_status, nullable=False, server_default="not_required"),
    )
    op.execute("UPDATE order_items SET quantity_from_stock = quantity")
    op.create_check_constraint("order_item_quantity_from_stock_nonnegative", "order_items", "quantity_from_stock >= 0")
    op.create_check_constraint("order_item_quantity_to_order_nonnegative", "order_items", "quantity_to_order >= 0")
    op.create_check_constraint("order_item_quantity_received_nonnegative", "order_items", "quantity_received_for_order >= 0")
    op.create_check_constraint("order_item_fulfillment_quantity_matches_ordered", "order_items", "quantity_from_stock + quantity_to_order = quantity")
    op.create_check_constraint("order_item_received_not_more_than_shortage", "order_items", "quantity_received_for_order <= quantity_to_order")

    op.create_table(
        "inventory_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", document_status, nullable=False, server_default="draft"),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("posted_idempotency_key", sa.String(length=128), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["posted_by_admin_id"], ["admin_users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_receipts_idempotency_key"),
        sa.UniqueConstraint(
            "posted_idempotency_key",
            name="uq_inventory_receipts_posted_idempotency_key",
        ),
    )
    op.create_table(
        "inventory_counts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", document_status, nullable=False, server_default="draft"),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("posted_idempotency_key", sa.String(length=128), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["posted_by_admin_id"], ["admin_users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_counts_idempotency_key"),
        sa.UniqueConstraint(
            "posted_idempotency_key",
            name="uq_inventory_counts_posted_idempotency_key",
        ),
    )
    op.create_table(
        "inventory_receipt_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("purchase_cost", sa.Numeric(10, 2), nullable=False),
        sa.Column("lot_number", sa.String(length=100), nullable=True),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.CheckConstraint("quantity > 0", name="inventory_receipt_item_quantity_positive"),
        sa.CheckConstraint("purchase_cost >= 0", name="inventory_receipt_item_purchase_cost_nonnegative"),
        sa.ForeignKeyConstraint(["receipt_id"], ["inventory_receipts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_inventory_receipt_items_receipt_id", "inventory_receipt_items", ["receipt_id"])
    op.create_index("ix_inventory_receipt_items_product_id", "inventory_receipt_items", ["product_id"])
    op.create_table(
        "inventory_count_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("count_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("expected_quantity", sa.Integer(), nullable=False),
        sa.Column("counted_quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("expected_quantity >= 0", name="inventory_count_item_expected_nonnegative"),
        sa.CheckConstraint("counted_quantity >= 0", name="inventory_count_item_counted_nonnegative"),
        sa.ForeignKeyConstraint(["count_id"], ["inventory_counts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("count_id", "product_id", name="uq_inventory_count_items_count_product"),
    )
    op.create_index("ix_inventory_count_items_count_id", "inventory_count_items", ["count_id"])
    op.create_index("ix_inventory_count_items_product_id", "inventory_count_items", ["product_id"])
    op.create_table(
        "inventory_receipt_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_item_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="inventory_receipt_allocation_quantity_positive"),
        sa.ForeignKeyConstraint(["receipt_item_id"], ["inventory_receipt_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "receipt_item_id",
            "order_item_id",
            name="uq_inventory_receipt_allocations_receipt_item_order_item",
        ),
    )
    op.create_index("ix_inventory_receipt_allocations_receipt_item_id", "inventory_receipt_allocations", ["receipt_item_id"])
    op.create_index("ix_inventory_receipt_allocations_order_item_id", "inventory_receipt_allocations", ["order_item_id"])
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("movement_type", movement_type, nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("on_hand_delta", sa.Integer(), nullable=False),
        sa.Column("reserved_delta", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("receipt_id", sa.Integer(), nullable=True),
        sa.Column("count_id", sa.Integer(), nullable=True),
        sa.Column("admin_user_id", sa.Integer(), nullable=True),
        sa.Column("reference_key", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="inventory_movement_quantity_positive"),
        sa.CheckConstraint("on_hand_delta <> 0 OR reserved_delta <> 0", name="inventory_movement_has_delta"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["receipt_id"], ["inventory_receipts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["count_id"], ["inventory_counts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("reference_key", name="uq_inventory_movements_reference_key"),
    )
    for column in ("product_id", "order_id", "order_item_id", "receipt_id", "count_id", "admin_user_id"):
        op.create_index(f"ix_inventory_movements_{column}", "inventory_movements", [column])


def downgrade() -> None:
    for column in ("admin_user_id", "count_id", "receipt_id", "order_item_id", "order_id", "product_id"):
        op.drop_index(f"ix_inventory_movements_{column}", table_name="inventory_movements")
    op.drop_table("inventory_movements")
    op.drop_index("ix_inventory_receipt_allocations_order_item_id", table_name="inventory_receipt_allocations")
    op.drop_index("ix_inventory_receipt_allocations_receipt_item_id", table_name="inventory_receipt_allocations")
    op.drop_table("inventory_receipt_allocations")
    op.drop_index("ix_inventory_count_items_product_id", table_name="inventory_count_items")
    op.drop_index("ix_inventory_count_items_count_id", table_name="inventory_count_items")
    op.drop_table("inventory_count_items")
    op.drop_index("ix_inventory_receipt_items_product_id", table_name="inventory_receipt_items")
    op.drop_index("ix_inventory_receipt_items_receipt_id", table_name="inventory_receipt_items")
    op.drop_table("inventory_receipt_items")
    op.drop_table("inventory_counts")
    op.drop_table("inventory_receipts")

    for constraint in (
        "order_item_received_not_more_than_shortage",
        "order_item_fulfillment_quantity_matches_ordered",
        "order_item_quantity_received_nonnegative",
        "order_item_quantity_to_order_nonnegative",
        "order_item_quantity_from_stock_nonnegative",
    ):
        op.drop_constraint(constraint, "order_items", type_="check")
    op.drop_column("order_items", "procurement_status")
    op.drop_column("order_items", "quantity_received_for_order")
    op.drop_column("order_items", "quantity_to_order")
    op.drop_column("order_items", "quantity_from_stock")

    for constraint in (
        "products_available_quantity_nonnegative",
        "products_reserved_quantity_nonnegative",
        "products_stock_quantity_nonnegative",
    ):
        op.drop_constraint(constraint, "products", type_="check")
    op.drop_constraint("uq_products_barcode", "products", type_="unique")
    op.drop_column("products", "allow_backorder")
    op.drop_column("products", "barcode")
    op.drop_column("products", "reserved_quantity")

    bind = op.get_bind()
    procurement_status.drop(bind, checkfirst=True)
    document_status.drop(bind, checkfirst=True)
    movement_type.drop(bind, checkfirst=True)
