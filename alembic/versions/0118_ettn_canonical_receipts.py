"""Link issued local ETTN truth to one canonical receipt; no backfill/dispatch."""
from alembic import op
import sqlalchemy as sa

revision = '0118_ettn_canonical_receipts'
down_revision = '0117_mock_ettn_automation'
branch_labels = None
depends_on = None


OWNER_0117 = """CREATE OR REPLACE FUNCTION exclusive_mock_fiscal_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM 1 FROM orders WHERE id = NEW.order_id FOR UPDATE;
  IF TG_TABLE_NAME = 'mock_fiscal_automations' THEN
    IF NOT EXISTS (SELECT 1 FROM order_payments WHERE id = NEW.payment_id AND order_id = NEW.order_id AND simulation = true)
      OR NOT EXISTS (SELECT 1 FROM order_shipments WHERE id = NEW.shipment_id AND order_id = NEW.order_id AND simulation = true AND carrier = 'mock_nova_poshta')
    THEN RAISE EXCEPTION 'Mock automation payment and shipment must belong to order'; END IF;
    IF NEW.status != 'cancelled' AND EXISTS (SELECT 1 FROM fiscal_documents
      WHERE (order_id = NEW.order_id OR payment_id = NEW.payment_id) AND kind = 'sale' AND status != 'cancelled')
    THEN RAISE EXCEPTION 'Manual fiscal owner already exists'; END IF;
  ELSIF NEW.kind = 'sale' AND NEW.status != 'cancelled' AND EXISTS (SELECT 1 FROM mock_fiscal_automations
      WHERE (order_id = NEW.order_id OR payment_id = NEW.payment_id) AND status != 'cancelled')
  THEN RAISE EXCEPTION 'Automation fiscal owner already exists'; END IF;
  RETURN NEW;
END $$"""


def upgrade():
    op.add_column('fiscal_documents', sa.Column('automation_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_fiscal_document_automation', 'fiscal_documents', 'mock_fiscal_automations', ['automation_id'], ['id'], ondelete='RESTRICT')
    op.create_unique_constraint('uq_fiscal_document_automation', 'fiscal_documents', ['automation_id'])
    op.execute("""CREATE OR REPLACE FUNCTION exclusive_mock_fiscal_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE project mock_fiscal_automations%ROWTYPE;
            truth mock_automation_records%ROWTYPE;
            expected jsonb;
    BEGIN
      PERFORM 1 FROM orders WHERE id = NEW.order_id FOR UPDATE;
      IF TG_TABLE_NAME = 'mock_fiscal_automations' THEN
        IF NOT EXISTS (SELECT 1 FROM order_payments WHERE id = NEW.payment_id AND order_id = NEW.order_id AND simulation = true)
          OR NOT EXISTS (SELECT 1 FROM order_shipments WHERE id = NEW.shipment_id AND order_id = NEW.order_id AND simulation = true AND carrier = 'mock_nova_poshta')
        THEN RAISE EXCEPTION 'Mock automation payment and shipment must belong to order'; END IF;
        IF NEW.status != 'cancelled' AND EXISTS (SELECT 1 FROM fiscal_documents
          WHERE (order_id = NEW.order_id OR payment_id = NEW.payment_id) AND kind = 'sale' AND status != 'cancelled'
            AND (automation_id IS NULL OR automation_id != NEW.id))
        THEN RAISE EXCEPTION 'Manual fiscal owner already exists'; END IF;
      ELSIF NEW.automation_id IS NOT NULL THEN
        SELECT * INTO project FROM mock_fiscal_automations WHERE id = NEW.automation_id;
        SELECT * INTO truth FROM mock_automation_records WHERE project_id = NEW.automation_id;
        IF project.id IS NULL OR truth.project_id IS NULL OR project.status != 'issued' OR truth.status != 'issued'
          OR NOT project.simulation OR NOT NEW.simulation OR NEW.kind != 'sale' OR NEW.status != 'issued'
          OR NEW.parent_sale_id IS NOT NULL OR NEW.return_id IS NOT NULL
          OR NEW.order_id != project.order_id OR NEW.payment_id != project.payment_id OR NEW.actor_id != project.actor_id
          OR NEW.provider_uuid != project.provider_uuid OR truth.provider_uuid != project.provider_uuid
          OR truth.snapshot_hash != project.snapshot_hash OR truth.external_id IS DISTINCT FROM project.external_id
          OR NEW.cash_register_id != project.cash_register_id OR NEW.shift_revision != project.shift_revision
          OR NEW.approved_at != project.approved_at OR truth.result::jsonb IS DISTINCT FROM project.result::jsonb
          OR project.result IS NULL OR project.result::jsonb = 'null'::jsonb
          OR project.result->>'provider_uuid' IS DISTINCT FROM project.provider_uuid
          OR project.result->>'snapshot_hash' IS DISTINCT FROM project.snapshot_hash
          OR project.result->>'external_id' IS DISTINCT FROM project.external_id
          OR project.result->>'payment_id' IS DISTINCT FROM project.payment_id::text
          OR project.result->>'shipment_id' IS DISTINCT FROM project.shipment_id::text
          OR project.result->>'amount_minor' IS DISTINCT FROM project.snapshot->>'amount_minor'
          OR project.result->>'tracking_number' IS DISTINCT FROM project.snapshot->>'tracking_number'
          OR project.result->>'condition' IS DISTINCT FROM project.snapshot->>'condition'
          OR project.result->>'document_kind' IS DISTINCT FROM 'mock_automation_result'
          OR project.result->>'simulation' IS DISTINCT FROM 'true' OR project.result->>'is_sent_dps' IS DISTINCT FROM 'false'
          OR project.result->>'is_fiscal_document' IS DISTINCT FROM 'false'
          OR project.result->>'provider_contract' IS DISTINCT FROM 'unknown'
          OR project.result->>'issued_at' IS NULL
          OR NEW.dispatched_at IS DISTINCT FROM (project.result->>'issued_at')::timestamptz
        THEN RAISE EXCEPTION 'Canonical mock automation receipt scope mismatch'; END IF;
        expected := project.snapshot::jsonb || jsonb_build_object(
          'document_kind', 'mock_fiscal_receipt',
          'notice', 'MOCK / СИМУЛЯЦІЯ. Не є фіскальним чеком. Не передається до ДПС.',
          'source', 'mock_ettn',
          'automation', jsonb_build_object('id', project.id, 'provider_uuid', project.provider_uuid,
            'approval_snapshot_hash', project.snapshot_hash, 'external_id', project.external_id,
            'shipment_id', project.shipment_id, 'tracking_number', project.snapshot->>'tracking_number',
            'condition', project.snapshot->>'condition'));
        IF NEW.snapshot::jsonb IS DISTINCT FROM expected OR NOT EXISTS (
          SELECT 1 FROM fiscal_previews WHERE id = NEW.preview_id AND order_id = project.order_id
            AND actor_id = project.actor_id AND cash_register_id = project.cash_register_id
            AND shift_revision = project.shift_revision AND snapshot::jsonb = expected AND snapshot_hash = NEW.snapshot_hash)
        THEN RAISE EXCEPTION 'Canonical mock automation receipt provenance mismatch'; END IF;
      ELSIF EXISTS (SELECT 1 FROM mock_fiscal_automations WHERE provider_uuid = NEW.provider_uuid)
      THEN RAISE EXCEPTION 'Automation source UUID requires its canonical link';
      ELSIF NEW.kind = 'sale' AND NEW.status != 'cancelled' AND EXISTS (SELECT 1 FROM mock_fiscal_automations
          WHERE (order_id = NEW.order_id OR payment_id = NEW.payment_id) AND status != 'cancelled')
      THEN RAISE EXCEPTION 'Automation fiscal owner already exists'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION protect_fiscal_automation_link() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND NEW.automation_id IS DISTINCT FROM OLD.automation_id
      THEN RAISE EXCEPTION 'Fiscal automation provenance link is immutable'; END IF;
      IF NEW.automation_id IS NULL AND (NEW.snapshot->>'source' = 'mock_ettn' OR NEW.snapshot::jsonb ? 'automation')
      THEN RAISE EXCEPTION 'Automation provenance requires immutable canonical link'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_fiscal_automation_link BEFORE INSERT OR UPDATE ON fiscal_documents FOR EACH ROW EXECUTE FUNCTION protect_fiscal_automation_link()')
    op.execute("""CREATE FUNCTION validate_canonical_mock_acceptance() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.automation_id IS NOT NULL AND (NOT EXISTS (
          SELECT 1 FROM mock_receipts WHERE document_id = NEW.id AND provider_uuid = NEW.provider_uuid
            AND snapshot_hash = NEW.snapshot_hash AND accepted_at = NEW.dispatched_at)
        OR EXISTS (SELECT 1 FROM fiscal_jobs WHERE document_id = NEW.id))
      THEN RAISE EXCEPTION 'Canonical automation receipt requires existing mock acceptance and no dispatch job'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE CONSTRAINT TRIGGER canonical_mock_acceptance AFTER INSERT OR UPDATE ON fiscal_documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_canonical_mock_acceptance()')
    op.execute("""CREATE FUNCTION prevent_automation_dispatch_job() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM fiscal_documents WHERE id = NEW.document_id AND automation_id IS NOT NULL)
      THEN RAISE EXCEPTION 'Canonical automation receipt cannot dispatch again'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER prevent_automation_dispatch_job BEFORE INSERT OR UPDATE ON fiscal_jobs FOR EACH ROW EXECUTE FUNCTION prevent_automation_dispatch_job()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM fiscal_documents WHERE automation_id IS NOT NULL)')).scalar():
        raise RuntimeError('Linked automation receipt history exists; downgrade refused')
    op.execute('DROP TRIGGER prevent_automation_dispatch_job ON fiscal_jobs')
    op.execute('DROP FUNCTION prevent_automation_dispatch_job()')
    op.execute('DROP TRIGGER canonical_mock_acceptance ON fiscal_documents')
    op.execute('DROP FUNCTION validate_canonical_mock_acceptance()')
    op.execute('DROP TRIGGER protect_fiscal_automation_link ON fiscal_documents')
    op.execute('DROP FUNCTION protect_fiscal_automation_link()')
    op.execute(OWNER_0117)
    op.drop_constraint('uq_fiscal_document_automation', 'fiscal_documents', type_='unique')
    op.drop_constraint('fk_fiscal_document_automation', 'fiscal_documents', type_='foreignkey')
    op.drop_column('fiscal_documents', 'automation_id')
