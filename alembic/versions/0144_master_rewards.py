"""Private manual master reward ledger. No backfill or sale/provider mutation."""
from alembic import op
import sqlalchemy as sa

revision = '0144_master_rewards'
down_revision = '0143_auto_checkout_checkpoint'
branch_labels = None
depends_on = None


def fk(column, table, **kw):
    return sa.Column(column, sa.Integer(), sa.ForeignKey(table + '.id', ondelete='RESTRICT'), nullable=False, **kw)


def upgrade():
    op.create_table('master_reward_policy', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('rate_bps', sa.Integer()),
        sa.CheckConstraint('id = 1 AND revision >= 0 AND (rate_bps IS NULL OR rate_bps BETWEEN 0 AND 10000)', name='valid'))
    op.execute('INSERT INTO master_reward_policy(id,revision,rate_bps) VALUES(1,0,NULL)')
    op.create_table('master_reward_entries', sa.Column('id', sa.Integer(), primary_key=True),
        fk('order_id','orders',index=True), fk('order_item_id','order_items',unique=True), fk('handover_id','order_handovers'),
        fk('master_id','masters',index=True), fk('beneficiary_user_id','admin_users',index=True),
        sa.Column('master_name',sa.String(255),nullable=False), fk('product_id','products'),
        sa.Column('product_name',sa.String(255),nullable=False), sa.Column('quantity',sa.Integer(),nullable=False),
        sa.Column('base_minor',sa.BigInteger(),nullable=False),sa.Column('rate_bps',sa.Integer(),nullable=False),
        sa.Column('reward_minor',sa.BigInteger(),nullable=False),sa.Column('policy_revision',sa.Integer(),nullable=False),
        sa.Column('sold_at',sa.DateTime(timezone=True),nullable=False,index=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),fk('created_by','admin_users'),
        sa.Column('needs_review',sa.Boolean(),nullable=False),sa.Column('revision',sa.Integer(),nullable=False),
        sa.CheckConstraint('quantity > 0 AND base_minor > 0 AND reward_minor >= 0 AND rate_bps BETWEEN 0 AND 10000 AND revision > 0', name='valid'))
    op.create_table('master_reward_settlements',sa.Column('id',sa.Integer(),primary_key=True),
        fk('master_id','masters',index=True),fk('beneficiary_user_id','admin_users',index=True),
        sa.Column('master_name',sa.String(255),nullable=False),sa.Column('total_minor',sa.BigInteger(),nullable=False),
        sa.Column('paid_at',sa.DateTime(timezone=True),nullable=False,index=True),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),fk('created_by','admin_users'),
        sa.CheckConstraint('total_minor > 0',name='positive'))
    op.create_table('master_reward_allocations',sa.Column('id',sa.Integer(),primary_key=True),
        fk('settlement_id','master_reward_settlements',index=True),fk('entry_id','master_reward_entries',unique=True))
    op.create_table('master_reward_audits',sa.Column('id',sa.Integer(),primary_key=True),fk('actor_id','admin_users'),
        sa.Column('operation',sa.String(32),nullable=False),sa.Column('reason',sa.Text(),nullable=False),
        sa.Column('facts',sa.JSON(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('master_reward_operations',sa.Column('id',sa.Integer(),primary_key=True),fk('actor_id','admin_users'),
        sa.Column('key',sa.String(36),nullable=False),sa.Column('operation',sa.String(32),nullable=False),
        sa.Column('payload_hash',sa.String(64),nullable=False),sa.Column('response',sa.JSON(),nullable=False),
        sa.UniqueConstraint('actor_id','key',name='uq_master_reward_operation_actor_key'))
    op.execute('''CREATE FUNCTION master_reward_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Private accounting snapshot is immutable'; END $$''')
    for table in ('master_reward_settlements','master_reward_allocations','master_reward_audits','master_reward_operations'):
        op.execute(f'CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION master_reward_immutable()')
    op.execute('''CREATE FUNCTION master_reward_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Private accounting snapshot is immutable'; END IF;
        IF (to_jsonb(NEW)-'revision'-'needs_review') IS DISTINCT FROM (to_jsonb(OLD)-'revision'-'needs_review')
          OR NEW.revision <> OLD.revision+1 THEN RAISE EXCEPTION 'Private accounting snapshot is immutable'; END IF;
        RETURN NEW;
      END $$''')
    op.execute('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON master_reward_entries FOR EACH ROW EXECUTE FUNCTION master_reward_entry_guard()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM master_reward_audits) OR EXISTS(SELECT 1 FROM master_reward_entries) OR EXISTS(SELECT 1 FROM master_reward_settlements)')).scalar():
        raise RuntimeError('Private reward history requires an explicit archival plan')
    for table in ('master_reward_operations','master_reward_audits','master_reward_allocations','master_reward_settlements','master_reward_entries'):
        op.execute(f'DROP TRIGGER immutable ON {table}')
    op.execute('DROP FUNCTION master_reward_entry_guard()')
    op.execute('DROP FUNCTION master_reward_immutable()')
    for table in ('master_reward_operations','master_reward_audits','master_reward_allocations','master_reward_settlements','master_reward_entries','master_reward_policy'):
        op.drop_table(table)
