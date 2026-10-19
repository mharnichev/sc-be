"""Stage 5 isolated mock returns, reports, audit and customer capabilities."""
from alembic import op
import sqlalchemy as sa

revision = '0116_mock_returns'
down_revision = '0115_real_np_tracking'
branch_labels = None
depends_on = None


def ref(name, table, *, unique=False, nullable=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(table + '.id', ondelete='RESTRICT'), nullable=nullable, unique=unique)


def ident():
    return sa.Column('id', sa.Integer(), primary_key=True)


def created():
    return sa.Column('created_at', sa.DateTime(timezone=True), nullable=False)


def simulation():
    return sa.Column('simulation', sa.Boolean(), nullable=False, server_default=sa.true())


def evidence():
    return sa.Column('evidence', sa.String(500), nullable=False)


def register():
    return sa.Column('cash_register_id', sa.String(32), sa.ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'), nullable=False)


FACTS = ('mock_returns', 'mock_refunds', 'mock_return_receipts', 'mock_fiscal_reports',
    'mock_reconciliation_acknowledgements', 'mock_fiscal_shares', 'mock_fiscal_share_revocations')


def upgrade():
    op.add_column('admin_users', sa.Column('can_fiscalize_returns', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('fiscal_documents', sa.Column('kind', sa.String(16), nullable=False, server_default='sale'))
    op.add_column('fiscal_documents', ref('parent_sale_id', 'fiscal_documents', nullable=True))
    op.create_table('mock_returns', ident(), ref('order_id', 'orders'), ref('parent_sale_id', 'fiscal_documents'),
        ref('actor_id', 'admin_users'), sa.Column('amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('items', sa.JSON(), nullable=False), sa.Column('reason', sa.String(500), nullable=False), simulation(), created(),
        sa.CheckConstraint('amount_minor >= 0 AND simulation = true', name='mock_return_only'))
    op.add_column('fiscal_documents', sa.Column('return_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_fiscal_document_return', 'fiscal_documents', 'mock_returns', ['return_id'], ['id'], ondelete='RESTRICT')
    op.create_check_constraint('fiscal_document_kind_scope', 'fiscal_documents',
        "(kind = 'sale' AND parent_sale_id IS NULL AND return_id IS NULL) OR (kind = 'return' AND parent_sale_id IS NOT NULL AND return_id IS NOT NULL)")
    for field in ('order', 'payment'):
        op.drop_index('uq_fiscal_active_' + field, table_name='fiscal_documents')
        op.create_index('uq_fiscal_active_' + field, 'fiscal_documents', [field + '_id'], unique=True,
            postgresql_where=sa.text("status != 'cancelled' AND kind = 'sale'"))
    op.create_index('uq_fiscal_active_return', 'fiscal_documents', ['return_id'], unique=True,
        postgresql_where=sa.text("status != 'cancelled' AND kind = 'return'"))
    op.create_table('mock_refunds', ident(), ref('return_id', 'mock_returns', unique=True),
        sa.Column('amount_minor', sa.BigInteger(), nullable=False), sa.Column('method', sa.String(16), nullable=False), evidence(),
        ref('operator_id', 'admin_users'), simulation(), created(),
        sa.CheckConstraint("amount_minor >= 0 AND simulation = true AND method IN ('cash', 'card')", name='mock_refund_only'))
    op.create_table('mock_return_receipts', ident(), ref('return_id', 'mock_returns', unique=True),
        sa.Column('items', sa.JSON(), nullable=False), evidence(), ref('operator_id', 'admin_users'), simulation(), created(),
        sa.CheckConstraint('simulation = true', name='mock_receipt_only'))
    op.create_table('mock_fiscal_reports', ident(), sa.Column('kind', sa.String(1), nullable=False), register(),
        sa.Column('shift_revision', sa.Integer(), nullable=False), ref('actor_id', 'admin_users'),
        sa.Column('totals', sa.JSON(), nullable=False), created(), sa.CheckConstraint("kind IN ('x', 'z')", name='mock_report_kind'))
    op.create_index('uq_mock_z_report', 'mock_fiscal_reports', ['cash_register_id', 'shift_revision'], unique=True, postgresql_where=sa.text("kind = 'z'"))
    op.create_table('mock_reconciliation_acknowledgements', ident(), sa.Column('case_type', sa.String(40), nullable=False),
        sa.Column('resource_id', sa.String(64), nullable=False), evidence(), ref('actor_id', 'admin_users'), created())
    op.create_table('mock_fiscal_shares', ident(), ref('document_id', 'fiscal_documents'),
        sa.Column('generation', sa.Integer(), nullable=False), sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('scope', sa.String(32), nullable=False), ref('actor_id', 'admin_users'), created(),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('document_id', 'generation', name='uq_mock_fiscal_share_generation'),
        sa.CheckConstraint("scope = 'mock_fiscal_document' AND generation > 0", name='mock_fiscal_share_scope'))
    op.create_table('mock_fiscal_share_revocations', ident(), ref('share_id', 'mock_fiscal_shares', unique=True), ref('actor_id', 'admin_users'), created())
    for table in FACTS:
        op.execute(f'CREATE TRIGGER immutable_mock_return_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')
    op.execute("""CREATE FUNCTION protect_fiscal_return_scope() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.kind IS DISTINCT FROM OLD.kind OR NEW.return_id IS DISTINCT FROM OLD.return_id
          OR NEW.parent_sale_id IS DISTINCT FROM OLD.parent_sale_id
        THEN RAISE EXCEPTION 'Fiscal document scope is immutable'; END IF;
        RETURN NEW;
      END $$""")
    op.execute('CREATE TRIGGER immutable_fiscal_return_scope BEFORE UPDATE ON fiscal_documents FOR EACH ROW EXECUTE FUNCTION protect_fiscal_return_scope()')


def downgrade():
    # Audit history must survive application rollback. Disposable empty scope only.
    for table in FACTS:
        if op.get_bind().execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM {table})')).scalar():
            raise RuntimeError('Stage 5 audit facts exist; downgrade refused to preserve financial history')
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM fiscal_documents WHERE kind = 'return')")).scalar():
        raise RuntimeError('Stage 5 return documents exist; downgrade refused to preserve financial history')
    op.execute('DROP TRIGGER immutable_fiscal_return_scope ON fiscal_documents')
    op.execute('DROP FUNCTION protect_fiscal_return_scope()')
    op.drop_constraint('fiscal_document_kind_scope', 'fiscal_documents', type_='check')
    op.drop_index('uq_fiscal_active_return', table_name='fiscal_documents')
    op.drop_constraint('fk_fiscal_document_return', 'fiscal_documents', type_='foreignkey')
    op.drop_column('fiscal_documents', 'return_id')
    for table in reversed(FACTS):
        op.drop_table(table)
    for field in ('order', 'payment'):
        op.drop_index('uq_fiscal_active_' + field, table_name='fiscal_documents')
        op.create_index('uq_fiscal_active_' + field, 'fiscal_documents', [field + '_id'], unique=True, postgresql_where=sa.text("status != 'cancelled'"))
    op.drop_column('fiscal_documents', 'parent_sale_id')
    op.drop_column('fiscal_documents', 'kind')
    op.drop_column('admin_users', 'can_fiscalize_returns')
