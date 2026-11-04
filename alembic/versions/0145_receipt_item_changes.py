"""Audited receipt commands and private receipt-derived product purchase cost."""
from alembic import op
import sqlalchemy as sa

revision = '0145_receipt_item_changes'
down_revision = '0144_master_rewards'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('inventory_receipts',sa.Column('revision',sa.Integer(),nullable=False,server_default='1'))
    op.create_check_constraint('receipt_revision_positive','inventory_receipts','revision > 0')
    op.add_column('products',sa.Column('purchase_unit_cost',sa.Numeric(10,2)))
    op.add_column('products',sa.Column('purchase_cost_receipt_id',sa.Integer(),sa.ForeignKey('inventory_receipts.id',ondelete='RESTRICT')))
    op.create_check_constraint('products_purchase_cost_nonnegative','products','purchase_unit_cost IS NULL OR purchase_unit_cost >= 0')
    op.create_table('inventory_receipt_changes',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('receipt_id',sa.Integer(),sa.ForeignKey('inventory_receipts.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('target_item_id',sa.Integer(),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_users.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('operation',sa.String(24),nullable=False),
        sa.Column('idempotency_key',sa.String(128),nullable=False),
        sa.Column('payload_hash',sa.String(64),nullable=False),
        sa.Column('reason',sa.String(255),nullable=False),
        sa.Column('applied_revision',sa.Integer(),nullable=False),
        sa.Column('before',sa.JSON(),nullable=False),sa.Column('after',sa.JSON(),nullable=False),
        sa.Column('on_hand_delta',sa.Integer(),nullable=False),
        sa.Column('purchase_value_delta',sa.Numeric(24,2),nullable=False),
        sa.Column('movement_id',sa.Integer(),sa.ForeignKey('inventory_movements.id',ondelete='RESTRICT')),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('receipt_id','idempotency_key',name='uq_receipt_changes_key'),
        sa.UniqueConstraint('receipt_id','applied_revision',name='uq_receipt_changes_revision'),
        sa.CheckConstraint("operation IN ('draft_update','draft_delete','posted_correction')",name='receipt_change_operation'),
        sa.CheckConstraint('applied_revision > 1',name='receipt_change_revision'))
    op.create_index('ix_inventory_receipt_changes_receipt_id','inventory_receipt_changes',['receipt_id'])
    op.execute("""CREATE FUNCTION guard_receipt_change_history() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Receipt change history is immutable'; END $$""")
    op.execute('CREATE TRIGGER immutable_receipt_change BEFORE UPDATE OR DELETE ON inventory_receipt_changes FOR EACH ROW EXECUTE FUNCTION guard_receipt_change_history()')
    op.execute("""CREATE FUNCTION guard_posted_receipt_header() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF OLD.status::text = 'posted' THEN
          IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Posted receipt is immutable'; END IF;
          IF (to_jsonb(NEW) - 'revision' - 'updated_at') IS DISTINCT FROM
             (to_jsonb(OLD) - 'revision' - 'updated_at') THEN
            RAISE EXCEPTION 'Posted receipt facts are immutable';
          END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$""")
    op.execute('CREATE TRIGGER immutable_posted_receipt BEFORE UPDATE OR DELETE ON inventory_receipts FOR EACH ROW EXECUTE FUNCTION guard_posted_receipt_header()')
    op.execute("""CREATE FUNCTION guard_posted_receipt_child() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE ids integer[]; parents integer[]; parent_record record;
      BEGIN
        IF TG_TABLE_NAME = 'inventory_receipt_items' THEN
          IF TG_OP = 'INSERT' THEN parents := ARRAY[NEW.receipt_id];
          ELSIF TG_OP = 'DELETE' THEN parents := ARRAY[OLD.receipt_id];
          ELSE parents := ARRAY[OLD.receipt_id, NEW.receipt_id]; END IF;
        ELSE
          IF TG_OP = 'INSERT' THEN ids := ARRAY[NEW.receipt_item_id];
          ELSIF TG_OP = 'DELETE' THEN ids := ARRAY[OLD.receipt_item_id];
          ELSE ids := ARRAY[OLD.receipt_item_id, NEW.receipt_item_id]; END IF;
          SELECT array_agg(receipt_id) INTO parents FROM inventory_receipt_items WHERE id=ANY(ids);
        END IF;
        FOR parent_record IN SELECT status::text AS status FROM inventory_receipts
            WHERE id=ANY(parents) ORDER BY id FOR UPDATE LOOP
          IF parent_record.status = 'posted' THEN
            RAISE EXCEPTION 'Posted receipt lines and allocations are immutable';
          END IF;
        END LOOP;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$""")
    for table in ['inventory_receipt_items','inventory_receipt_allocations']:
        op.execute(f'CREATE TRIGGER immutable_posted_receipt_child BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_posted_receipt_child()')


def downgrade():
    # Never erase audit or an initialized card cost during a schema rollback.
    bind = op.get_bind()
    used = bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM inventory_receipt_changes) OR EXISTS (SELECT 1 FROM products WHERE purchase_unit_cost IS NOT NULL OR purchase_cost_receipt_id IS NOT NULL)"))
    if used:
        raise RuntimeError("Cannot downgrade receipt changes after audit/cost data has been recorded")
    for table in ['inventory_receipt_items','inventory_receipt_allocations']:
        op.execute(f'DROP TRIGGER immutable_posted_receipt_child ON {table}')
    op.execute('DROP FUNCTION guard_posted_receipt_child()')
    op.execute('DROP TRIGGER immutable_posted_receipt ON inventory_receipts')
    op.execute('DROP FUNCTION guard_posted_receipt_header()')
    op.drop_table('inventory_receipt_changes')
    op.execute('DROP FUNCTION guard_receipt_change_history()')
    op.drop_constraint('products_purchase_cost_nonnegative','products',type_='check')
    op.drop_column('products','purchase_cost_receipt_id')
    op.drop_column('products','purchase_unit_cost')
    op.drop_constraint('receipt_revision_positive','inventory_receipts',type_='check')
    op.drop_column('inventory_receipts','revision')
