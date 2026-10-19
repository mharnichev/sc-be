"""Existing NP reverse parcels, separate inspection, linked Checkbox return claims."""
from alembic import op
import sqlalchemy as sa

revision="0127_shop_np_returns"
down_revision="0126_shop_payments"
branch_labels=None
depends_on=None


def ref(name,table,*,unique=False,nullable=False):
    return sa.Column(name,sa.Integer(),sa.ForeignKey(table+".id",ondelete="RESTRICT"),nullable=nullable,unique=unique)


def audit():
    return [ref("operator_id","admin_users"),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False)]


def snapshot():
    return [sa.Column("snapshot",sa.JSON(),nullable=False),sa.Column("snapshot_hash",sa.String(64),nullable=False)]


NP_RETURN_PREDICATE="""((h.kind='pickup' AND s.fulfillment_method='pickup' AND NEW.carrier_receipt_id IS NULL)
 OR (h.kind='nova_poshta' AND s.fulfillment_method='nova_poshta' AND EXISTS(
 SELECT 1 FROM shop_np_return_receipts nr JOIN shop_np_return_cases nc ON nc.id=nr.case_id
 WHERE nr.id=NEW.carrier_receipt_id AND nc.order_id=NEW.order_id AND nc.sale_document_id=NEW.sale_document_id
 AND nc.financial_mode='paid' AND nc.register_id=NEW.register_id AND nc.register_profile_hash=r.profile_hash
 AND nc.fulfillment_id=h.id AND nc.lines::jsonb=NEW.lines::jsonb
 AND (NOT nc.test_context OR r.authority='test'))))"""

NP_PHYSICAL_JOIN="""LEFT JOIN shop_checkbox_physical_receipts h ON h.return_id=r.id
       LEFT JOIN shop_np_return_receipts nr ON nr.id=r.carrier_receipt_id
       LEFT JOIN shop_np_return_cases nc ON nc.id=nr.case_id AND nc.order_id=r.order_id AND nc.sale_document_id=r.sale_document_id
        AND nc.financial_mode='paid' AND nc.register_id=r.register_id AND nc.lines::jsonb=r.lines::jsonb"""
NP_PHYSICAL_CONDITION="""WHERE r.id=NEW.return_id AND ((r.carrier_receipt_id IS NULL AND h.id IS NOT NULL)
        OR (r.carrier_receipt_id IS NOT NULL AND nr.id IS NOT NULL AND nc.id IS NOT NULL))"""


def replace_function(name,old,new):
    value=op.get_bind().execute(sa.text("SELECT pg_get_functiondef(to_regprocedure(:name))"),{"name":name+"()"}).scalar()
    if not value or value.count(old)!=1:
        raise RuntimeError("Unexpected installed function while extending "+name)
    op.execute(value.replace(old,new,1))


def upgrade():
    op.create_table("shop_np_return_cases",sa.Column("id",sa.Integer(),primary_key=True),ref("order_id","orders",unique=True),
        ref("original_shipment_id","order_shipments",unique=True),ref("fulfillment_id","shop_fulfillment_facts"),
        ref("sale_document_id","shop_checkbox_documents",nullable=True),
        sa.Column("register_id",sa.String(100),sa.ForeignKey("shop_checkbox_registers.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("register_profile_hash",sa.String(64),nullable=False),sa.Column("original_ttn",sa.String(14),nullable=False),
        sa.Column("return_ttn",sa.String(14),nullable=False,unique=True),sa.Column("financial_mode",sa.String(16),nullable=False),
        sa.Column("linkage_kind",sa.String(24),nullable=False),sa.Column("manual_link",sa.JSON(),nullable=True),
        sa.Column("profile_id",sa.String(64),nullable=False),sa.Column("profile",sa.JSON(),nullable=False),sa.Column("profile_hash",sa.String(64),nullable=False),
        sa.Column("authority",sa.String(16),nullable=False),sa.Column("transport",sa.String(16),nullable=False),sa.Column("test_context",sa.Boolean(),nullable=False),
        sa.Column("revision",sa.Integer(),nullable=False),sa.Column("lines",sa.JSON(),nullable=False),*snapshot(),
        sa.Column("reason",sa.String(500),nullable=False),*audit())
    op.create_table("shop_np_return_observations",sa.Column("id",sa.Integer(),primary_key=True),ref("case_id","shop_np_return_cases"),
        sa.Column("proof",sa.JSON(),nullable=False),sa.Column("proof_canonical",sa.Text(),nullable=False),sa.Column("proof_hash",sa.String(64),nullable=False),*audit())
    op.create_table("shop_np_return_receipts",sa.Column("id",sa.Integer(),primary_key=True),ref("case_id","shop_np_return_cases",unique=True),
        ref("observation_id","shop_np_return_observations"),sa.Column("lines",sa.JSON(),nullable=False),*snapshot(),sa.Column("evidence",sa.String(500),nullable=False),*audit())
    op.create_table("shop_np_unpaid_closures",sa.Column("id",sa.Integer(),primary_key=True),ref("case_id","shop_np_return_cases",unique=True),
        ref("order_id","orders",unique=True),ref("receipt_id","shop_np_return_receipts",unique=True),*snapshot(),sa.Column("evidence",sa.String(500),nullable=False),*audit())
    op.create_table("shop_np_return_audits",sa.Column("id",sa.Integer(),primary_key=True),ref("case_id","shop_np_return_cases"),
        sa.Column("request_key",sa.String(64),nullable=False,unique=True),sa.Column("operation",sa.String(32),nullable=False),sa.Column("facts",sa.JSON(),nullable=False),*audit())
    op.add_column("shop_checkbox_returns",sa.Column("carrier_receipt_id",sa.Integer(),nullable=True))
    op.create_foreign_key("fk_checkbox_np_receipt","shop_checkbox_returns","shop_np_return_receipts",["carrier_receipt_id"],["id"],ondelete="RESTRICT")
    op.create_unique_constraint("uq_checkbox_np_receipt","shop_checkbox_returns",["carrier_receipt_id"])
    for table in ("shop_np_return_observations","shop_np_return_receipts","shop_np_unpaid_closures","shop_np_return_audits"):
        op.execute(f"CREATE TRIGGER immutable_np_return BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION protect_np_return_case() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     IF TG_OP='UPDATE' AND ((to_jsonb(NEW)-'revision') IS DISTINCT FROM (to_jsonb(OLD)-'revision') OR NEW.revision!=OLD.revision+1)
     THEN RAISE EXCEPTION 'NP return case identity and selected contents are immutable'; END IF;
     IF TG_OP='INSERT' THEN
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      PERFORM 1 FROM shop_checkbox_registers WHERE id=NEW.register_id FOR UPDATE;
      PERFORM pg_advisory_xact_lock(hashtextextended('np-return-ttn:'||NEW.return_ttn,0));
      IF NEW.original_ttn !~ '^[0-9]{14}$' OR NEW.return_ttn !~ '^[0-9]{14}$' OR NEW.original_ttn=NEW.return_ttn
       OR EXISTS(SELECT 1 FROM order_shipments WHERE tracking_number=NEW.return_ttn OR provider_reference=NEW.return_ttn)
       OR NEW.profile_id !~ '^[A-Za-z0-9_-]{1,64}$' OR NEW.profile::jsonb->>'id' IS DISTINCT FROM NEW.profile_id
       OR NEW.profile::jsonb->'approved' IS DISTINCT FROM 'true'::jsonb
       OR NEW.profile::jsonb->>'environment' IS NULL OR NEW.profile::jsonb->>'environment' NOT IN ('test','production')
       OR jsonb_typeof(NEW.profile::jsonb->'manual_link_allowed') IS DISTINCT FROM 'boolean'
       OR NEW.profile_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.profile::jsonb),'UTF8')),'hex')
       OR NEW.snapshot_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.snapshot::jsonb),'UTF8')),'hex')
       OR NEW.authority NOT IN ('test','provider') OR NEW.transport IS DISTINCT FROM (CASE WHEN NEW.authority='test' THEN 'mock_http' ELSE 'https' END)
       OR NEW.authority='provider' AND NEW.profile::jsonb->>'environment' IS DISTINCT FROM 'production'
       OR NEW.financial_mode NOT IN ('paid','unpaid_cod') OR NEW.linkage_kind NOT IN ('easy_return','manual_document')
       OR NEW.revision!=1 OR COALESCE(btrim(NEW.reason),'')=''
       OR NEW.snapshot::jsonb->>'order_id' IS DISTINCT FROM NEW.order_id::text
       OR NEW.snapshot::jsonb->>'original_shipment_id' IS DISTINCT FROM NEW.original_shipment_id::text
       OR NEW.snapshot::jsonb->>'fulfillment_id' IS DISTINCT FROM NEW.fulfillment_id::text
       OR NEW.snapshot::jsonb->>'original_ttn' IS DISTINCT FROM NEW.original_ttn OR NEW.snapshot::jsonb->>'return_ttn' IS DISTINCT FROM NEW.return_ttn
       OR NEW.snapshot::jsonb->'lines' IS DISTINCT FROM NEW.lines::jsonb
       OR NEW.snapshot::jsonb->>'authority' IS DISTINCT FROM NEW.authority OR NEW.snapshot::jsonb->'test_context' IS DISTINCT FROM to_jsonb(NEW.test_context)
       OR NEW.snapshot::jsonb->>'profile_hash' IS DISTINCT FROM NEW.profile_hash
       OR NEW.snapshot::jsonb->'manual_link' IS DISTINCT FROM COALESCE(NEW.manual_link::jsonb,'null'::jsonb)
       OR NOT EXISTS(SELECT 1 FROM orders o JOIN shop_fulfillment_facts f ON f.order_id=o.id
         JOIN order_shipments s ON s.id=f.shipment_id JOIN shop_checkbox_bindings b ON b.order_id=o.id
         JOIN shop_checkbox_registers r ON r.id=b.register_id
         WHERE o.id=NEW.order_id AND o.channel='shop' AND NOT o.simulation AND o.fulfillment_method='nova_poshta'
          AND o.status::text!='cancelled' AND f.id=NEW.fulfillment_id AND f.kind='nova_poshta' AND s.id=NEW.original_shipment_id
          AND NOT s.simulation AND s.active AND s.carrier='nova_poshta' AND s.source='attach' AND s.return_of_shipment_id IS NULL
          AND s.tracking_number=NEW.original_ttn AND s.provider_reference=NEW.original_ttn
          AND r.id=NEW.register_id AND r.profile_hash=NEW.register_profile_hash AND b.profile_hash=r.profile_hash AND b.authority=r.authority
          AND (NOT NEW.test_context OR r.authority='test')
          AND NEW.test_context=(NEW.authority='test' OR r.authority='test' OR f.authority='test' OR EXISTS(
            SELECT 1 FROM shop_payment_intents pi WHERE pi.order_id=o.id AND (pi.authority='test' OR pi.profile->>'environment'='test')))
          AND (NEW.authority='test' OR r.authority!='test' AND f.authority!='test')
          AND NEW.snapshot::jsonb->>'dispatched_at' IS NOT NULL AND (NEW.snapshot::jsonb->>'dispatched_at')::timestamptz=f.created_at
          AND NOT EXISTS(SELECT 1 FROM shop_checkbox_operations q WHERE q.register_id=r.id AND q.status IN ('queued','unknown','pending')))
       OR EXISTS(SELECT 1 FROM shop_carrier_returns WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM shop_fiscal_documents WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM shop_financial_returns WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM qr_payment_attempts WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM order_shipments WHERE order_id=NEW.order_id AND id!=NEW.original_shipment_id)
       OR EXISTS(SELECT 1 FROM inventory_movements m WHERE m.movement_type::text='customer_return' AND (m.order_id=NEW.order_id
          OR m.order_item_id IN (SELECT id FROM order_items WHERE order_id=NEW.order_id)))
      THEN RAISE EXCEPTION 'NP return requires an exclusive canonical original parcel and pinned profile'; END IF;
      IF NEW.linkage_kind='manual_document' THEN
       IF NEW.profile::jsonb->'manual_link_allowed' IS DISTINCT FROM 'true'::jsonb OR jsonb_typeof(NEW.manual_link::jsonb) IS DISTINCT FROM 'object'
        OR NEW.manual_link::jsonb->'verified' IS DISTINCT FROM 'true'::jsonb
        OR NEW.manual_link::jsonb->>'order_id' IS DISTINCT FROM NEW.order_id::text
        OR NEW.manual_link::jsonb->>'original_shipment_id' IS DISTINCT FROM NEW.original_shipment_id::text
        OR NEW.manual_link::jsonb->>'original_ttn' IS DISTINCT FROM NEW.original_ttn OR NEW.manual_link::jsonb->>'return_ttn' IS DISTINCT FROM NEW.return_ttn
        OR COALESCE(btrim(NEW.manual_link::jsonb->>'source'),'')='' OR COALESCE(NEW.manual_link::jsonb->>'document_id','') !~ '^[A-Za-z0-9_.:/-]{1,100}$'
        OR COALESCE(NEW.manual_link::jsonb->>'line_id','') !~ '^[A-Za-z0-9_.:/-]{1,100}$'
       THEN RAISE EXCEPTION 'Manual NP linkage requires separate exact documentary attestation'; END IF;
      ELSIF COALESCE(NEW.manual_link::jsonb,'null'::jsonb)!='null'::jsonb THEN RAISE EXCEPTION 'Provider linkage cannot contain manual proof'; END IF;
     END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_np_return_case BEFORE INSERT OR UPDATE ON shop_np_return_cases FOR EACH ROW EXECUTE FUNCTION protect_np_return_case()")
    op.execute("CREATE TRIGGER immutable_np_return_case BEFORE DELETE ON shop_np_return_cases FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION validate_np_observation() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE c shop_np_return_cases%ROWTYPE; previous shop_np_return_observations%ROWTYPE; k text; v jsonb; older jsonb;
    BEGIN
     SELECT * INTO c FROM shop_np_return_cases WHERE id=NEW.case_id FOR UPDATE;
     SELECT * INTO previous FROM shop_np_return_observations WHERE case_id=NEW.case_id ORDER BY id DESC LIMIT 1;
     IF c.id IS NULL OR jsonb_typeof(NEW.proof::jsonb) IS DISTINCT FROM 'object'
      OR NEW.proof::jsonb->'verified' IS DISTINCT FROM 'true'::jsonb OR NEW.proof::jsonb->>'profile_id' IS DISTINCT FROM c.profile_id
      OR NEW.proof::jsonb->>'authority' IS DISTINCT FROM c.authority OR NEW.proof::jsonb->>'transport' IS DISTINCT FROM c.transport
      OR NEW.proof::jsonb->>'original_ttn' IS DISTINCT FROM c.original_ttn OR NEW.proof::jsonb->>'return_ttn' IS DISTINCT FROM c.return_ttn
      OR NEW.proof::jsonb->>'linkage_kind' IS DISTINCT FROM c.linkage_kind
      OR NEW.proof::jsonb->'linkage_verified' IS DISTINCT FROM to_jsonb(c.linkage_kind='easy_return')
      OR NEW.proof::jsonb->>'linkage_authority' IS DISTINCT FROM (CASE WHEN c.linkage_kind='manual_document' THEN 'manual' ELSE c.authority END)
      OR NEW.proof_canonical IS DISTINCT FROM shop_payment_canonical(NEW.proof::jsonb)
      OR NEW.proof_hash IS DISTINCT FROM encode(sha256(convert_to(NEW.proof_canonical,'UTF8')),'hex')
      OR NEW.proof::jsonb->>'proof_hash' IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.proof::jsonb-'proof_hash'),'UTF8')),'hex')
      OR NEW.proof::jsonb->>'observed_at' IS NULL OR (NEW.proof::jsonb->>'observed_at')::timestamptz<now()-interval '2 hours'
      OR (NEW.proof::jsonb->>'observed_at')::timestamptz>now()+interval '30 seconds'
      OR previous.id IS NOT NULL AND (NEW.proof::jsonb->>'observed_at')::timestamptz<(previous.proof->>'observed_at')::timestamptz
     THEN RAISE EXCEPTION 'NP observation requires exact fresh typed authenticated provenance'; END IF;
     FOREACH k IN ARRAY ARRAY['original','reverse'] LOOP
      v=NEW.proof::jsonb->k;older=previous.proof::jsonb->k;
      IF jsonb_typeof(v) IS DISTINCT FROM 'object' OR v->>'number' IS DISTINCT FROM (CASE WHEN k='original' THEN c.original_ttn ELSE c.return_ttn END)
       OR v->>'status' IS NULL OR v->>'status' IN ('unknown','requires_review') OR v->>'raw_status' IS NULL
       OR v->>'event_at' IS NOT NULL AND (v->>'event_at')::timestamptz>(NEW.proof::jsonb->>'observed_at')::timestamptz+interval '30 seconds'
       OR older->>'event_at' IS NOT NULL AND (v->>'event_at' IS NULL OR (v->>'event_at')::timestamptz<(older->>'event_at')::timestamptz
          OR (v->>'event_at')::timestamptz=(older->>'event_at')::timestamptz AND (v->>'raw_status') IS DISTINCT FROM (older->>'raw_status'))
      THEN RAISE EXCEPTION 'NP observation cannot regress or contradict a dated parcel event'; END IF;
     END LOOP;
     IF c.financial_mode='unpaid_cod' AND (NEW.proof::jsonb->'original'->>'event_at' IS NULL
       OR (NEW.proof::jsonb->'original'->>'event_at')::timestamptz<(c.snapshot->>'dispatched_at')::timestamptz-interval '30 seconds'
       OR (NEW.proof::jsonb->'original'->>'raw_status') IS DISTINCT FROM '103' AND NOT (
          NEW.proof::jsonb->'original'->>'raw_status'='102' AND EXISTS(SELECT 1 FROM shop_np_return_observations o WHERE o.case_id=c.id AND o.proof->'original'->>'raw_status'='103')))
     THEN RAISE EXCEPTION 'Unpaid NP refusal requires a durable dated original 103 chain'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_np_observation BEFORE INSERT ON shop_np_return_observations FOR EACH ROW EXECUTE FUNCTION validate_np_observation()")
    op.execute("""CREATE FUNCTION validate_np_case_contents() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE line jsonb; original jsonb; sale_state jsonb; shipped bigint;
    BEGIN
     IF NOT EXISTS(SELECT 1 FROM shop_np_return_observations WHERE case_id=NEW.id)
      OR jsonb_typeof(NEW.lines::jsonb) IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.lines::jsonb)=0
      OR (SELECT count(DISTINCT v->>'order_item_id') FROM jsonb_array_elements(NEW.lines::jsonb) v)!=jsonb_array_length(NEW.lines::jsonb)
     THEN RAISE EXCEPTION 'NP case requires one exact observed parcel allocation'; END IF;
     IF NEW.financial_mode='paid' THEN
      SELECT o.snapshot::jsonb INTO sale_state FROM shop_checkbox_documents d JOIN shop_checkbox_operations o ON o.id=d.operation_id
       JOIN shop_payment_confirmations p ON p.id=d.payment_confirmation_id
       WHERE d.id=NEW.sale_document_id AND d.order_id=NEW.order_id AND d.kind='sale' AND o.status='accepted' AND o.register_id=NEW.register_id
       AND p.order_id=NEW.order_id AND EXISTS(SELECT 1 FROM shop_checkbox_acceptances a WHERE a.operation_id=o.id AND a.snapshot_hash=o.snapshot_hash);
      IF sale_state IS NULL OR EXISTS(SELECT 1 FROM shop_checkbox_returns WHERE order_id=NEW.order_id AND carrier_receipt_id IS NULL)
       OR (SELECT COALESCE(sum((v->>'total_minor')::bigint),0) FROM jsonb_array_elements(NEW.lines::jsonb) v)<=0
      THEN RAISE EXCEPTION 'Paid NP parcel requires its accepted exact original sale'; END IF;
      IF sale_state->'payment'->>'type'='CASH' AND
        (SELECT COALESCE(sum((v->>'total_minor')::bigint),0) FROM jsonb_array_elements(NEW.lines::jsonb) v)%100!=0
      THEN RAISE EXCEPTION 'NP cash parcel requires a supported whole-hryvnia refund amount'; END IF;
     ELSIF NEW.sale_document_id IS NOT NULL OR EXISTS(SELECT 1 FROM order_payments WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM shop_payment_intents WHERE order_id=NEW.order_id) OR EXISTS(SELECT 1 FROM shop_completions WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM shop_checkbox_documents WHERE order_id=NEW.order_id) OR EXISTS(SELECT 1 FROM shop_checkbox_returns WHERE order_id=NEW.order_id)
       OR NOT EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND payment_method IN ('cod','cashOnDelivery','cardOnDelivery','cash_on_delivery','card_on_delivery'))
       OR (SELECT count(*) FROM order_items WHERE order_id=NEW.order_id)!=jsonb_array_length(NEW.lines::jsonb)
     THEN RAISE EXCEPTION 'Unpaid NP parcel must cover a full COD order without financial facts'; END IF;
     FOR line IN SELECT value FROM jsonb_array_elements(NEW.lines::jsonb) LOOP
      IF jsonb_typeof(line->'order_item_id') IS DISTINCT FROM 'number' OR jsonb_typeof(line->'product_id') IS DISTINCT FROM 'number'
       OR jsonb_typeof(line->'quantity') IS DISTINCT FROM 'number' OR COALESCE(line->>'quantity','') !~ '^[1-9][0-9]*$'
       OR NOT EXISTS(SELECT 1 FROM order_items i JOIN shipment_items a ON a.order_item_id=i.id
        WHERE i.id=(line->>'order_item_id')::int AND i.order_id=NEW.order_id AND i.product_id=(line->>'product_id')::int
        AND a.shipment_id=NEW.original_shipment_id AND a.quantity=i.quantity AND (line->>'quantity')::int<=i.quantity
        AND (NEW.financial_mode='paid' OR (line->>'quantity')::int=i.quantity))
      THEN RAISE EXCEPTION 'NP parcel lines must preserve the exact canonical dispatch'; END IF;
      SELECT COALESCE(sum(-on_hand_delta),0) INTO shipped FROM inventory_movements WHERE order_id=NEW.order_id
       AND order_item_id=(line->>'order_item_id')::int AND product_id=(line->>'product_id')::int AND movement_type::text='shipment'
       AND reference_key='order:'||NEW.order_id::text||':item:'||(line->>'order_item_id')||':'||'shipment';
      IF shipped<(line->>'quantity')::int THEN RAISE EXCEPTION 'NP parcel exceeds canonical shipped stock'; END IF;
      IF NEW.financial_mode='paid' THEN
       SELECT value INTO original FROM jsonb_array_elements(sale_state->'items') WHERE value->>'order_item_id'=line->>'order_item_id';
       IF original IS NULL OR (line-ARRAY['quantity','total_minor']) IS DISTINCT FROM (original-ARRAY['quantity','total_minor'])
        OR jsonb_typeof(line->'total_minor') IS DISTINCT FROM 'number'
        OR (line->>'total_minor')::bigint IS DISTINCT FROM floor((original->>'total_minor')::numeric*(line->>'quantity')::bigint/(original->>'quantity')::bigint)::bigint
       THEN RAISE EXCEPTION 'NP paid parcel must preserve original discounted kopeck and tax allocation'; END IF;
      END IF;
     END LOOP;
     RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_np_case_contents AFTER INSERT ON shop_np_return_cases DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_np_case_contents()")
    op.execute("""CREATE FUNCTION validate_np_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE c shop_np_return_cases%ROWTYPE; o shop_np_return_observations%ROWTYPE; l jsonb; selected jsonb; count_moves bigint;
    BEGIN
     SELECT * INTO c FROM shop_np_return_cases WHERE id=NEW.case_id FOR UPDATE;
     PERFORM 1 FROM shop_checkbox_registers WHERE id=c.register_id FOR UPDATE;
     SELECT * INTO o FROM shop_np_return_observations WHERE id=NEW.observation_id AND case_id=c.id;
     IF c.id IS NULL OR o.id IS NULL OR NEW.snapshot_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.snapshot::jsonb),'UTF8')),'hex')
      OR EXISTS(SELECT 1 FROM shop_np_return_observations newer WHERE newer.case_id=c.id AND newer.id>NEW.observation_id)
      OR NEW.snapshot::jsonb IS DISTINCT FROM jsonb_build_object('case_id',c.id,'case_hash',c.snapshot_hash,'observation_id',o.id,'lines',NEW.lines::jsonb,'contents_verified',true,'test_context',c.test_context)
      OR o.proof->'reverse'->>'raw_status' NOT IN ('9','10','11') OR o.proof->'reverse'->>'raw_status' IS NULL
      OR o.proof->'reverse'->>'status' IS DISTINCT FROM 'delivered' OR o.proof->'reverse'->>'event_at' IS NULL
      OR (o.proof->>'observed_at')::timestamptz<now()-interval '2 hours'
      OR (o.proof->'reverse'->>'event_at')::timestamptz<(c.snapshot->>'dispatched_at')::timestamptz-interval '30 seconds'
      OR jsonb_typeof(NEW.lines::jsonb) IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.lines::jsonb)!=jsonb_array_length(c.lines::jsonb)
      OR (SELECT count(DISTINCT v->>'order_item_id') FROM jsonb_array_elements(NEW.lines::jsonb) v)!=jsonb_array_length(NEW.lines::jsonb)
      OR COALESCE(btrim(NEW.evidence),'')=''
     THEN RAISE EXCEPTION 'NP physical receipt requires dated return delivery and separate exact content attestation'; END IF;
     FOR l IN SELECT value FROM jsonb_array_elements(NEW.lines::jsonb) LOOP
      SELECT value INTO selected FROM jsonb_array_elements(c.lines::jsonb) WHERE value->>'order_item_id'=l->>'order_item_id';
      IF selected IS NULL OR jsonb_typeof(l->'quantity') IS DISTINCT FROM 'number' OR l->>'quantity' IS DISTINCT FROM selected->>'quantity'
        OR NOT COALESCE(l->>'disposition' IN ('restock','not_restock'),false)
      THEN RAISE EXCEPTION 'NP physical receipt must consume every selected quantity and disposition once'; END IF;
      SELECT count(*) INTO count_moves FROM inventory_movements WHERE reference_key='shop-np-return:'||NEW.id::text||':'||(l->>'order_item_id');
      IF l->>'disposition'='restock' AND (count_moves!=1 OR NOT EXISTS(SELECT 1 FROM inventory_movements m WHERE
        m.reference_key='shop-np-return:'||NEW.id::text||':'||(l->>'order_item_id') AND m.order_id=c.order_id AND m.order_item_id=(l->>'order_item_id')::int
        AND m.product_id=(selected->>'product_id')::int AND m.movement_type::text='customer_return' AND m.quantity=(l->>'quantity')::int
        AND m.on_hand_delta=(l->>'quantity')::int AND m.reserved_delta=0 AND m.admin_user_id=NEW.operator_id))
       OR l->>'disposition'='not_restock' AND count_moves!=0
      THEN RAISE EXCEPTION 'NP inspection requires its exact once-only stock ledger'; END IF;
     END LOOP;
     RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_np_receipt AFTER INSERT ON shop_np_return_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_np_receipt()")
    op.execute("""CREATE FUNCTION shop_np_unpaid_can_close(oid int,cid int) RETURNS boolean LANGUAGE SQL STABLE AS $$
     SELECT EXISTS(SELECT 1 FROM shop_np_return_cases c JOIN shop_np_return_receipts r ON r.case_id=c.id
      JOIN shop_np_unpaid_closures x ON x.case_id=c.id AND x.receipt_id=r.id JOIN orders o ON o.id=c.order_id
      WHERE c.id=cid AND c.order_id=oid AND x.order_id=oid AND c.financial_mode='unpaid_cod'
       AND o.payment_method IN ('cod','cashOnDelivery','cardOnDelivery','cash_on_delivery','card_on_delivery')
       AND NOT EXISTS(SELECT 1 FROM order_payments WHERE order_id=oid) AND NOT EXISTS(SELECT 1 FROM shop_payment_intents WHERE order_id=oid)
       AND NOT EXISTS(SELECT 1 FROM qr_payment_attempts WHERE order_id=oid) AND NOT EXISTS(SELECT 1 FROM shop_completions WHERE order_id=oid)
       AND NOT EXISTS(SELECT 1 FROM shop_checkbox_documents WHERE order_id=oid) AND NOT EXISTS(SELECT 1 FROM shop_checkbox_returns WHERE order_id=oid)
       AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=oid AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(c.lines::jsonb) l
          WHERE l->>'order_item_id'=i.id::text AND l->>'product_id'=i.product_id::text AND (l->>'quantity')::int=i.quantity)))
    $$""")
    op.execute("""CREATE FUNCTION validate_np_closure() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE c shop_np_return_cases%ROWTYPE; r shop_np_return_receipts%ROWTYPE;
    BEGIN
     SELECT * INTO c FROM shop_np_return_cases WHERE id=NEW.case_id;SELECT * INTO r FROM shop_np_return_receipts WHERE id=NEW.receipt_id;
     IF NOT shop_np_unpaid_can_close(NEW.order_id,NEW.case_id) OR r.case_id IS DISTINCT FROM c.id
      OR NEW.snapshot::jsonb IS DISTINCT FROM jsonb_build_object('case_id',c.id,'order_id',NEW.order_id,'receipt_id',r.id,'case_hash',c.snapshot_hash,'receipt_hash',r.snapshot_hash,'test_context',c.test_context)
      OR NEW.snapshot_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.snapshot::jsonb),'UTF8')),'hex')
      OR NOT EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND status::text='cancelled')
     THEN RAISE EXCEPTION 'Unpaid NP closure requires exact full physical receipt without financial facts'; END IF;RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_np_closure AFTER INSERT ON shop_np_unpaid_closures DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_np_closure()")
    replace_function("protect_shop_checkbox_return","h.kind='pickup' AND s.fulfillment_method='pickup'",NP_RETURN_PREDICATE)
    replace_function("validate_shop_checkbox_document","JOIN shop_checkbox_physical_receipts h ON h.return_id=r.id",NP_PHYSICAL_JOIN)
    replace_function("validate_shop_checkbox_document","WHERE r.id=NEW.return_id",NP_PHYSICAL_CONDITION)
    replace_function("validate_shop_checkbox_physical","PERFORM 1 FROM shop_checkbox_registers WHERE id=q.register_id FOR UPDATE;",
        "PERFORM 1 FROM shop_checkbox_registers WHERE id=q.register_id FOR UPDATE; IF q.carrier_receipt_id IS NOT NULL THEN RAISE EXCEPTION 'NP physical receipt is already canonical; no second pickup receipt or restock is allowed'; END IF;")
    replace_function("protect_shop_order","AND shop_unpaid_return_can_close(OLD.id,c.case_id))",
        "AND shop_unpaid_return_can_close(OLD.id,c.case_id)) AND NOT EXISTS(SELECT 1 FROM shop_np_unpaid_closures nc WHERE nc.order_id=OLD.id AND shop_np_unpaid_can_close(OLD.id,nc.case_id))")
    op.execute("""CREATE FUNCTION protect_np_return_context() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE oid int;
    BEGIN
     IF TG_TABLE_NAME='orders' THEN oid=OLD.id;ELSE oid=NEW.order_id;END IF;
     IF TG_TABLE_NAME='shop_checkbox_operations' THEN
      PERFORM 1 FROM shop_checkbox_registers WHERE id=NEW.register_id FOR UPDATE;
     ELSIF TG_TABLE_NAME!='orders' THEN
      PERFORM 1 FROM orders WHERE id=oid FOR UPDATE;
     END IF;
     IF TG_TABLE_NAME='orders' THEN
      IF EXISTS(SELECT 1 FROM shop_np_unpaid_closures WHERE order_id=oid) AND NEW.status::text!='cancelled'
       OR EXISTS(SELECT 1 FROM shop_np_return_cases WHERE order_id=oid) AND
        (NEW.total_amount IS DISTINCT FROM OLD.total_amount OR NEW.subtotal_amount IS DISTINCT FROM OLD.subtotal_amount OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount
         OR NEW.channel IS DISTINCT FROM OLD.channel OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method
         OR NEW.payment_method IS DISTINCT FROM OLD.payment_method)
      THEN RAISE EXCEPTION 'NP return order binding and unpaid closure cannot be rewritten'; END IF;
     ELSIF TG_TABLE_NAME='shop_checkbox_operations' THEN
      IF EXISTS(SELECT 1 FROM shop_np_return_cases c WHERE c.order_id=oid AND c.test_context) AND NEW.authority IS DISTINCT FROM 'test'
      THEN RAISE EXCEPTION 'Test NP return context cannot become production fiscal proof'; END IF;
      IF NEW.kind IN ('z','shift_close') AND EXISTS(SELECT 1 FROM shop_np_return_cases c WHERE c.register_id=NEW.register_id
        AND NOT EXISTS(SELECT 1 FROM shop_np_unpaid_closures x WHERE x.case_id=c.id)
        AND NOT EXISTS(SELECT 1 FROM shop_np_return_receipts nr JOIN shop_checkbox_returns q ON q.carrier_receipt_id=nr.id
          JOIN shop_checkbox_refund_facts f ON f.return_id=q.id JOIN shop_checkbox_documents d ON d.return_id=q.id AND d.kind='return'
          JOIN shop_checkbox_operations o ON o.id=d.operation_id AND o.status='accepted'
          JOIN shop_checkbox_acceptances a ON a.operation_id=o.id AND a.snapshot_hash=o.snapshot_hash WHERE nr.case_id=c.id))
      THEN RAISE EXCEPTION 'Unresolved NP return obligations block fiscal shift closing'; END IF;
     ELSIF EXISTS(SELECT 1 FROM shop_np_return_cases WHERE order_id=oid) THEN
      RAISE EXCEPTION 'NP return order cannot receive new money, handover or completion facts';
     END IF;RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_np_return_context BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION protect_np_return_context()")
    op.execute("CREATE TRIGGER protect_np_return_context BEFORE INSERT OR UPDATE ON shop_checkbox_operations FOR EACH ROW EXECUTE FUNCTION protect_np_return_context()")
    for table in ("order_payments","shop_payment_confirmations","order_handovers","shop_completions","shop_payment_intents",
        "shop_carrier_returns","shop_fiscal_documents","shop_financial_returns","fiscal_documents","qr_payment_attempts"):
        op.execute(f"CREATE TRIGGER protect_np_return_context BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION protect_np_return_context()")
    op.execute("""CREATE FUNCTION protect_np_return_stock() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE oid int;
    BEGIN
     IF NEW.movement_type::text!='customer_return' THEN RETURN NEW;END IF;
     SELECT order_id INTO oid FROM order_items WHERE id=NEW.order_item_id;
     oid=COALESCE(oid,NEW.order_id);
     PERFORM 1 FROM orders WHERE id=oid OR id=NEW.order_id ORDER BY id FOR UPDATE;
     IF EXISTS(SELECT 1 FROM shop_np_return_cases WHERE order_id=oid OR order_id=NEW.order_id)
      AND NOT EXISTS(SELECT 1 FROM shop_np_return_receipts r JOIN shop_np_return_cases c ON c.id=r.case_id,
       jsonb_array_elements(r.lines::jsonb) l,jsonb_array_elements(c.lines::jsonb) s
       WHERE c.order_id=NEW.order_id AND l->>'order_item_id'=NEW.order_item_id::text AND s->>'order_item_id'=l->>'order_item_id'
        AND l->>'disposition'='restock' AND NEW.reference_key='shop-np-return:'||r.id::text||':'||NEW.order_item_id::text
        AND NEW.product_id=(s->>'product_id')::int AND NEW.quantity=(l->>'quantity')::int AND NEW.on_hand_delta=NEW.quantity AND NEW.reserved_delta=0 AND NEW.admin_user_id=r.operator_id)
     THEN RAISE EXCEPTION 'NP returned stock requires its sole canonical inspected parcel ledger'; END IF;RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_np_return_stock BEFORE INSERT ON inventory_movements FOR EACH ROW EXECUTE FUNCTION protect_np_return_stock()")
    op.execute("""CREATE FUNCTION protect_np_return_shipment() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE identity_change boolean;old_oid int;
    BEGIN
     identity_change=TG_OP='INSERT' OR NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.tracking_number IS DISTINCT FROM OLD.tracking_number
       OR NEW.provider_reference IS DISTINCT FROM OLD.provider_reference OR NEW.simulation IS DISTINCT FROM OLD.simulation
       OR NEW.carrier IS DISTINCT FROM OLD.carrier OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.return_of_shipment_id IS DISTINCT FROM OLD.return_of_shipment_id OR NEW.active IS DISTINCT FROM OLD.active;
     IF NOT identity_change THEN RETURN NEW;END IF;
     IF TG_OP='UPDATE' THEN old_oid=OLD.order_id;END IF;
     PERFORM 1 FROM orders WHERE id=old_oid OR id=NEW.order_id ORDER BY id FOR UPDATE;
     IF (TG_OP='INSERT' OR NEW.order_id IS DISTINCT FROM OLD.order_id) AND EXISTS(SELECT 1 FROM shop_np_return_cases WHERE order_id=NEW.order_id)
     THEN RAISE EXCEPTION 'NP return order cannot adopt an additional shipment'; END IF;
     IF TG_OP='INSERT' OR NEW.tracking_number IS DISTINCT FROM OLD.tracking_number OR NEW.provider_reference IS DISTINCT FROM OLD.provider_reference THEN
      PERFORM pg_advisory_xact_lock(hashtextextended('np-return-ttn:'||COALESCE(NEW.tracking_number,NEW.provider_reference),0));
      IF EXISTS(SELECT 1 FROM shop_np_return_cases WHERE return_ttn=NEW.tracking_number OR return_ttn=NEW.provider_reference)
      THEN RAISE EXCEPTION 'An NP reverse TTN cannot be adopted by another shipment'; END IF;
     END IF;
     IF TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM shop_np_return_cases WHERE original_shipment_id=OLD.id) AND
       (NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.tracking_number IS DISTINCT FROM OLD.tracking_number OR NEW.provider_reference IS DISTINCT FROM OLD.provider_reference
        OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.carrier IS DISTINCT FROM OLD.carrier OR NEW.source IS DISTINCT FROM OLD.source
        OR NEW.return_of_shipment_id IS DISTINCT FROM OLD.return_of_shipment_id OR NEW.active IS DISTINCT FROM OLD.active)
     THEN RAISE EXCEPTION 'NP original dispatch identity is immutable'; END IF;RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_np_return_shipment BEFORE INSERT OR UPDATE ON order_shipments FOR EACH ROW EXECUTE FUNCTION protect_np_return_shipment()")
    op.execute("""CREATE FUNCTION protect_np_return_items() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE old_oid int;new_oid int;
    BEGIN
     IF TG_TABLE_NAME='order_items' THEN
      IF TG_OP!='INSERT' THEN old_oid=OLD.order_id;END IF;
      IF TG_OP!='DELETE' THEN new_oid=NEW.order_id;END IF;
     ELSE
      IF TG_OP!='INSERT' THEN SELECT order_id INTO old_oid FROM order_shipments WHERE id=OLD.shipment_id;END IF;
      IF TG_OP!='DELETE' THEN SELECT order_id INTO new_oid FROM order_shipments WHERE id=NEW.shipment_id;END IF;
     END IF;
     PERFORM 1 FROM orders WHERE id IN (old_oid,new_oid) ORDER BY id FOR UPDATE;
     IF TG_TABLE_NAME='shipment_items' THEN
      IF EXISTS(SELECT 1 FROM shop_np_return_cases WHERE order_id IN (old_oid,new_oid)) THEN
       RAISE EXCEPTION 'NP returned original parcel allocations are immutable';END IF;
     ELSIF EXISTS(SELECT 1 FROM shop_np_return_cases WHERE order_id IN (old_oid,new_oid)) AND
       (TG_OP!='UPDATE' OR NEW.order_id IS DISTINCT FROM OLD.order_id
        OR NEW.product_id IS DISTINCT FROM OLD.product_id OR NEW.quantity IS DISTINCT FROM OLD.quantity
        OR NEW.price IS DISTINCT FROM OLD.price OR NEW.total_price IS DISTINCT FROM OLD.total_price OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount)
     THEN RAISE EXCEPTION 'NP returned order and original parcel allocations are immutable'; END IF;
     IF TG_OP='DELETE' THEN RETURN OLD;ELSE RETURN NEW;END IF;
    END $$""")
    for table in ("order_items","shipment_items"):
        op.execute(f"CREATE TRIGGER protect_np_return_items BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION protect_np_return_items()")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM shop_np_return_cases)")).scalar():
        raise RuntimeError("Accepted Shop NP return history exists; downgrade refused")
    replace_function("protect_shop_checkbox_return",NP_RETURN_PREDICATE,"h.kind='pickup' AND s.fulfillment_method='pickup'")
    replace_function("validate_shop_checkbox_document",NP_PHYSICAL_JOIN,"JOIN shop_checkbox_physical_receipts h ON h.return_id=r.id")
    replace_function("validate_shop_checkbox_document",NP_PHYSICAL_CONDITION,"WHERE r.id=NEW.return_id")
    replace_function("validate_shop_checkbox_physical"," IF q.carrier_receipt_id IS NOT NULL THEN RAISE EXCEPTION 'NP physical receipt is already canonical; no second pickup receipt or restock is allowed'; END IF;","")
    replace_function("protect_shop_order"," AND NOT EXISTS(SELECT 1 FROM shop_np_unpaid_closures nc WHERE nc.order_id=OLD.id AND shop_np_unpaid_can_close(OLD.id,nc.case_id))","")
    for table in ("orders","shop_checkbox_operations","order_payments","shop_payment_confirmations","order_handovers","shop_completions","shop_payment_intents",
        "shop_carrier_returns","shop_fiscal_documents","shop_financial_returns","fiscal_documents","qr_payment_attempts"):
        op.execute(f"DROP TRIGGER protect_np_return_context ON {table}")
    for table in ("order_items","shipment_items"):
        op.execute(f"DROP TRIGGER protect_np_return_items ON {table}")
    for trigger,table in (("protect_np_return_stock","inventory_movements"),("protect_np_return_shipment","order_shipments"),
        ("validate_np_closure","shop_np_unpaid_closures"),("validate_np_receipt","shop_np_return_receipts"),
        ("validate_np_case_contents","shop_np_return_cases"),("validate_np_observation","shop_np_return_observations"),
        ("protect_np_return_case","shop_np_return_cases"),("immutable_np_return_case","shop_np_return_cases")):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for table in ("shop_np_return_observations","shop_np_return_receipts","shop_np_unpaid_closures","shop_np_return_audits"):
        op.execute(f"DROP TRIGGER immutable_np_return ON {table}")
    for function in ("protect_np_return_items()","protect_np_return_stock()","protect_np_return_shipment()","protect_np_return_context()","validate_np_closure()",
        "shop_np_unpaid_can_close(int,int)","validate_np_receipt()","validate_np_case_contents()","validate_np_observation()","protect_np_return_case()"):
        op.execute("DROP FUNCTION "+function)
    op.drop_constraint("uq_checkbox_np_receipt","shop_checkbox_returns",type_="unique")
    op.drop_constraint("fk_checkbox_np_receipt","shop_checkbox_returns",type_="foreignkey")
    op.drop_column("shop_checkbox_returns","carrier_receipt_id")
    for table in ("shop_np_return_audits","shop_np_unpaid_closures","shop_np_return_receipts","shop_np_return_observations","shop_np_return_cases"):
        op.drop_table(table)
