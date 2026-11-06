"""Quick POS durable intent identity, no financial backfill."""
from alembic import op
import sqlalchemy as sa
revision='0147_pos_checkout_intents'
down_revision='0146_merge_inventory_booking'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('pos_checkout_intents',sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_users.id',ondelete='RESTRICT'),nullable=False,index=True),
        sa.Column('intent_id',sa.String(36),nullable=False),
        sa.Column('order_id',sa.Integer(),sa.ForeignKey('orders.id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('payload_hash',sa.String(64),nullable=False),sa.Column('kind',sa.String(16),nullable=False),
        sa.Column('employee_snapshot',sa.JSON(),nullable=False),sa.Column('product_snapshot',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('actor_id','intent_id',name='uq_pos_actor_intent'))
    op.execute('''CREATE FUNCTION pos_intent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'POS intent identity is immutable'; END IF;
        IF NEW.kind NOT IN ('quick','adopt') OR NOT EXISTS (SELECT 1 FROM orders
          WHERE id=NEW.order_id AND created_by_user_id=NEW.actor_id AND channel='backoffice_pos' AND NOT simulation)
        THEN RAISE EXCEPTION 'POS intent owner mismatch'; END IF;
        RETURN NEW;
      END $$''')
    op.execute('CREATE TRIGGER immutable BEFORE INSERT OR UPDATE OR DELETE ON pos_checkout_intents FOR EACH ROW EXECUTE FUNCTION pos_intent_guard()')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM pos_checkout_intents)')).scalar():
        raise RuntimeError('Accepted POS intents require archival; downgrade refused')
    op.execute('DROP TRIGGER immutable ON pos_checkout_intents')
    op.execute('DROP FUNCTION pos_intent_guard()')
    op.drop_table('pos_checkout_intents')
