"""Explicit full pre-handover cancellations and append-only invoice reconciliation."""
from alembic import op
import sqlalchemy as sa
revision='0128_shop_payment_resolutions'
down_revision='0127_shop_np_returns'
branch_labels=None
depends_on=None

def execute_sql(value):
    op.get_bind().exec_driver_sql(value.replace('%','%%'))

def replace(name,old,new):
    value=op.get_bind().execute(sa.text('SELECT pg_get_functiondef(to_regprocedure(:n))'),{'n':name+'()'}).scalar()
    if not value or value.count(old)!=1:raise RuntimeError('Unexpected function while extending '+name)
    execute_sql(value.replace(old,new,1))

def upgrade():
    op.execute('CREATE TABLE shop_resolution_migration_functions (name text PRIMARY KEY, definition text NOT NULL)')
    for name in ('protect_shop_checkbox_return','validate_shop_checkbox_document','validate_shop_checkbox_physical','protect_shop_order','protect_shop_r2_order','validate_shop_payment_evidence'):
        op.get_bind().execute(sa.text('INSERT INTO shop_resolution_migration_functions SELECT :n,pg_get_functiondef(to_regprocedure(:p))'),{'n':name,'p':name+'()'})
    execute_sql('''
CREATE TABLE shop_payment_resolution_cases (
 id serial PRIMARY KEY,order_id int NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
 kind varchar(24) NOT NULL,intent_id varchar(36) REFERENCES shop_payment_intents(id) ON DELETE RESTRICT,
 payment_confirmation_id int REFERENCES shop_payment_confirmations(id) ON DELETE RESTRICT,
 register_id varchar(100) REFERENCES shop_checkbox_registers(id) ON DELETE RESTRICT,
 profile_id varchar(100) NOT NULL,profile json NOT NULL,profile_hash varchar(64) NOT NULL,
 protocol varchar(40) NOT NULL,test_context boolean NOT NULL,revision int NOT NULL,snapshot json NOT NULL,snapshot_hash varchar(64) NOT NULL,
 reason varchar(500) NOT NULL,operator_id int NOT NULL REFERENCES admin_users(id),created_at timestamptz NOT NULL,
 CONSTRAINT uq_payment_resolution_kind UNIQUE(order_id,kind));
CREATE TABLE shop_payment_resolution_observations (
 id serial PRIMARY KEY,case_id int NOT NULL REFERENCES shop_payment_resolution_cases(id),invoice_id varchar(100) NOT NULL,error_code varchar(64),
 proof json,proof_canonical text NOT NULL,proof_hash varchar(64) NOT NULL,operator_id int NOT NULL REFERENCES admin_users(id),created_at timestamptz NOT NULL);
CREATE TABLE shop_payment_invoice_corrections (
 id serial PRIMARY KEY,case_id int NOT NULL UNIQUE REFERENCES shop_payment_resolution_cases(id),intent_id varchar(36) NOT NULL UNIQUE REFERENCES shop_payment_intents(id),
 profile_id varchar(64) NOT NULL,original_invoice_id varchar(100) NOT NULL,replacement_invoice_id varchar(100) NOT NULL,
 foreign_observation_id int NOT NULL REFERENCES shop_payment_resolution_observations(id),replacement_observation_id int NOT NULL REFERENCES shop_payment_resolution_observations(id),
 snapshot json NOT NULL,snapshot_hash varchar(64) NOT NULL,operator_id int NOT NULL REFERENCES admin_users(id),created_at timestamptz NOT NULL,
 CONSTRAINT uq_corrected_invoice UNIQUE(profile_id,replacement_invoice_id));
CREATE TABLE shop_payment_bank_closures (
 id serial PRIMARY KEY,case_id int NOT NULL UNIQUE REFERENCES shop_payment_resolution_cases(id),policy json NOT NULL,policy_hash varchar(64) NOT NULL,identity varchar(64) NOT NULL UNIQUE,
 snapshot json NOT NULL,snapshot_hash varchar(64) NOT NULL,operator_id int NOT NULL REFERENCES admin_users(id),created_at timestamptz NOT NULL);
CREATE TABLE shop_payment_resolution_finalizations (
 id serial PRIMARY KEY,case_id int NOT NULL UNIQUE REFERENCES shop_payment_resolution_cases(id),order_id int NOT NULL UNIQUE REFERENCES orders(id),
 snapshot json NOT NULL,snapshot_hash varchar(64) NOT NULL,operator_id int NOT NULL REFERENCES admin_users(id),created_at timestamptz NOT NULL);
CREATE TABLE shop_payment_financial_exceptions (
 id serial PRIMARY KEY,case_id int NOT NULL REFERENCES shop_payment_resolution_cases(id),observation_id int NOT NULL UNIQUE REFERENCES shop_payment_resolution_observations(id),
 reason varchar(64) NOT NULL,amount_minor bigint NOT NULL,currency varchar(3),operator_id int NOT NULL REFERENCES admin_users(id),created_at timestamptz NOT NULL);
ALTER TABLE shop_checkbox_returns ADD COLUMN cancellation_case_id int UNIQUE REFERENCES shop_payment_resolution_cases(id);
ALTER TABLE shop_payment_evidence ADD COLUMN correction_id int UNIQUE REFERENCES shop_payment_invoice_corrections(id);
CREATE FUNCTION shop_resolution_hash(v jsonb) RETURNS text LANGUAGE SQL IMMUTABLE AS $$ SELECT encode(sha256(convert_to(shop_payment_canonical(v),'UTF8')),'hex') $$;
CREATE FUNCTION shop_resolution_reference(v text) RETURNS text LANGUAGE SQL IMMUTABLE AS $$ SELECT CASE WHEN translate(regexp_replace(v,'^urn:uuid:','','i'),'{}-','') ~* '^[0-9a-f]{32}$' THEN translate(regexp_replace(v,'^urn:uuid:','','i'),'{}-','')::uuid::text ELSE v END $$;
CREATE FUNCTION shop_resolution_test(oid int) RETURNS boolean LANGUAGE SQL STABLE AS $$
 SELECT EXISTS(SELECT 1 FROM shop_payment_resolution_cases c LEFT JOIN shop_payment_bank_closures b ON b.case_id=c.id WHERE c.order_id=oid AND (c.test_context OR b.policy->>'environment'='test')) $$;
CREATE FUNCTION shop_resolution_active(oid int) RETURNS boolean LANGUAGE SQL STABLE AS $$
 SELECT EXISTS(SELECT 1 FROM shop_payment_resolution_cases c WHERE c.order_id=oid AND (EXISTS(SELECT 1 FROM shop_payment_financial_exceptions e WHERE e.case_id=c.id)
 OR NOT EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations f WHERE f.case_id=c.id) AND (c.kind='paid_cancellation' OR NOT EXISTS(SELECT 1 FROM shop_payment_evidence e WHERE e.order_id=oid)))) $$;
CREATE FUNCTION shop_resolution_no_handover(oid int) RETURNS boolean LANGUAGE SQL STABLE AS $$
 SELECT NOT EXISTS(SELECT 1 FROM order_handovers WHERE order_id=oid) AND NOT EXISTS(SELECT 1 FROM shop_fulfillment_facts WHERE order_id=oid)
 AND NOT EXISTS(SELECT 1 FROM shop_completions WHERE order_id=oid) AND NOT EXISTS(SELECT 1 FROM order_shipments WHERE order_id=oid AND
 (handed_over_at IS NOT NULL OR tracking_status IS NOT NULL AND tracking_status NOT IN ('unknown','created','not_found','preparing','cancelled','deleted')))
 AND NOT EXISTS(SELECT 1 FROM shipment_tracking_events e JOIN order_shipments s ON s.id=e.shipment_id WHERE s.order_id=oid AND e.normalized_status NOT IN ('unknown','created','not_found','preparing','cancelled','deleted'))
 AND NOT EXISTS(SELECT 1 FROM shipment_events e JOIN order_shipments s ON s.id=e.shipment_id WHERE s.order_id=oid AND e.normalized_status NOT IN ('unknown','created','not_found','preparing','cancelled','deleted'))
 AND NOT EXISTS(SELECT 1 FROM inventory_movements m WHERE (m.order_id=oid OR m.order_item_id IN (SELECT id FROM order_items WHERE order_id=oid)) AND (m.movement_type::text IN ('shipment','customer_return') OR m.on_hand_delta<0)) $$;
CREATE FUNCTION shop_resolution_can_close(oid int) RETURNS boolean LANGUAGE SQL STABLE AS $$
 SELECT EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations f JOIN shop_payment_resolution_cases c ON c.id=f.case_id
 WHERE f.order_id=oid AND c.order_id=oid AND shop_resolution_no_handover(oid) AND
 ((c.kind='invoice_review' AND EXISTS(SELECT 1 FROM shop_payment_bank_closures b WHERE b.case_id=c.id) AND NOT EXISTS(SELECT 1 FROM order_payments WHERE order_id=oid))
 OR (c.kind='paid_cancellation' AND EXISTS(SELECT 1 FROM shop_checkbox_returns q JOIN shop_checkbox_refund_facts r ON r.return_id=q.id
 JOIN shop_checkbox_documents d ON d.return_id=q.id AND d.kind='return' JOIN shop_checkbox_operations o ON o.id=d.operation_id AND o.status='accepted'
 JOIN shop_checkbox_acceptances a ON a.operation_id=o.id AND a.snapshot_hash=o.snapshot_hash WHERE q.cancellation_case_id=c.id AND q.order_id=oid
 AND q.amount_minor=(c.snapshot->>'amount_minor')::bigint AND r.amount_minor=q.amount_minor)))) $$;
CREATE FUNCTION protect_shop_resolution_case() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p shop_payment_intents%ROWTYPE;r shop_checkbox_registers%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' THEN
  IF to_jsonb(NEW)-'revision' IS DISTINCT FROM to_jsonb(OLD)-'revision' OR NEW.revision!=OLD.revision+1 THEN RAISE EXCEPTION 'Resolution binding is immutable';END IF;RETURN NEW;
 END IF;
 PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
 PERFORM 1 FROM shop_checkbox_registers WHERE id=NEW.register_id FOR UPDATE;
 SELECT * INTO p FROM shop_payment_intents WHERE id=NEW.intent_id;
 SELECT * INTO r FROM shop_checkbox_registers WHERE id=NEW.register_id;
 IF NEW.kind NOT IN ('paid_cancellation','invoice_review') OR NEW.protocol IS DISTINCT FROM (CASE WHEN NEW.kind='paid_cancellation' THEN 'full_sale_then_return' ELSE 'inspect_correct_or_bank_closure' END)
 OR NEW.profile_hash IS DISTINCT FROM shop_resolution_hash(NEW.profile::jsonb) OR NEW.snapshot_hash IS DISTINCT FROM shop_resolution_hash(NEW.snapshot::jsonb)
 OR NEW.profile->>'id' IS DISTINCT FROM NEW.profile_id OR NEW.revision!=1 OR COALESCE(btrim(NEW.reason),'')=''
 OR NEW.snapshot::jsonb->'goods_in_custody' IS DISTINCT FROM 'true'::jsonb OR NEW.snapshot->>'order_id' IS DISTINCT FROM NEW.order_id::text
 OR NEW.snapshot::jsonb->'test_context' IS DISTINCT FROM to_jsonb(NEW.test_context) OR NEW.snapshot->>'currency' IS DISTINCT FROM 'UAH'
 OR jsonb_typeof(NEW.snapshot::jsonb->'amount_minor') IS DISTINCT FROM 'number' OR COALESCE(NEW.snapshot->>'amount_minor','') !~ '^[1-9][0-9]*$'
 OR NOT shop_resolution_no_handover(NEW.order_id) OR shop_resolution_active(NEW.order_id)
 OR NOT EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND channel='shop' AND NOT simulation AND status::text NOT IN ('cancelled','completed') AND round(total_amount*100)::bigint=(NEW.snapshot->>'amount_minor')::bigint)
 OR EXISTS(SELECT 1 FROM shop_np_return_cases WHERE order_id=NEW.order_id) OR EXISTS(SELECT 1 FROM shop_carrier_returns WHERE order_id=NEW.order_id)
 OR EXISTS(SELECT 1 FROM shop_checkbox_returns WHERE order_id=NEW.order_id) OR EXISTS(SELECT 1 FROM qr_payment_attempts WHERE order_id=NEW.order_id)
 OR EXISTS(SELECT 1 FROM shop_fiscal_documents WHERE order_id=NEW.order_id) OR EXISTS(SELECT 1 FROM shop_financial_returns WHERE order_id=NEW.order_id)
 OR EXISTS(SELECT 1 FROM shop_checkbox_operations q WHERE q.register_id=NEW.register_id AND q.status IN ('queued','unknown','pending')
 AND NOT (NEW.kind='paid_cancellation' AND q.kind='sale' AND q.order_id=NEW.order_id AND EXISTS(SELECT 1 FROM shop_checkbox_documents d WHERE d.operation_id=q.id AND d.kind='sale' AND d.order_id=NEW.order_id AND d.payment_confirmation_id=NEW.payment_confirmation_id)))
 THEN RAISE EXCEPTION 'Resolution requires exclusive canonical pre-handover goods and immutable binding';END IF;
 IF NEW.kind='paid_cancellation' THEN
  IF r.id IS NULL OR NEW.profile::jsonb IS DISTINCT FROM r.profile::jsonb OR NEW.profile_hash IS DISTINCT FROM r.profile_hash
   OR NOT EXISTS(SELECT 1 FROM shop_payment_confirmations WHERE id=NEW.payment_confirmation_id AND order_id=NEW.order_id AND snapshot_hash=shop_resolution_hash(snapshot::jsonb))
   OR NOT EXISTS(SELECT 1 FROM shop_checkbox_bindings WHERE order_id=NEW.order_id AND register_id=r.id AND profile_hash=r.profile_hash AND authority=r.authority)
   OR NEW.test_context IS DISTINCT FROM (r.authority='test' OR COALESCE(p.authority='test',false) OR COALESCE(p.profile->>'environment'='test',false))
  THEN RAISE EXCEPTION 'Paid cancellation requires original full payment and R1 binding';END IF;
 ELSE
  IF p.id IS NULL OR p.order_id IS DISTINCT FROM NEW.order_id OR p.mode IS DISTINCT FROM 'prepayment' OR p.invoice_id IS NULL
   OR NEW.profile::jsonb IS DISTINCT FROM p.profile::jsonb OR NEW.profile_hash IS DISTINCT FROM p.profile_hash
   OR NEW.test_context IS DISTINCT FROM (p.authority='test' OR p.profile->>'environment'='test') OR NEW.payment_confirmation_id IS NOT NULL
   OR EXISTS(SELECT 1 FROM order_payments WHERE order_id=NEW.order_id)
   OR NEW.snapshot->>'intent_id' IS DISTINCT FROM p.id OR NEW.snapshot->>'reference' IS DISTINCT FROM p.reference OR NEW.snapshot->>'original_invoice_id' IS DISTINCT FROM p.invoice_id
  THEN RAISE EXCEPTION 'Invoice review requires the unpaid original immutable intent';END IF;
 END IF;RETURN NEW;
END $$;
CREATE TRIGGER protect_shop_resolution_case BEFORE INSERT OR UPDATE ON shop_payment_resolution_cases FOR EACH ROW EXECUTE FUNCTION protect_shop_resolution_case();
CREATE TRIGGER immutable_shop_resolution_case BEFORE DELETE ON shop_payment_resolution_cases FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation();
CREATE FUNCTION validate_shop_resolution_observation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c shop_payment_resolution_cases%ROWTYPE;b shop_payment_intents%ROWTYPE;p jsonb;k text;
BEGIN
 SELECT * INTO c FROM shop_payment_resolution_cases WHERE id=NEW.case_id FOR UPDATE;SELECT * INTO b FROM shop_payment_intents WHERE id=c.intent_id;p=NEW.proof::jsonb;
 IF c.kind IS DISTINCT FROM 'invoice_review' OR NEW.invoice_id !~ '^[A-Za-z0-9_-]{1,100}$' OR NEW.proof_canonical IS DISTINCT FROM shop_payment_canonical(COALESCE(p,'null'::jsonb))
 OR NEW.proof_hash IS DISTINCT FROM shop_resolution_hash(COALESCE(p,'null'::jsonb)) THEN RAISE EXCEPTION 'Invalid inspection provenance';END IF;
 IF p IS NULL OR p='null'::jsonb THEN
  IF NEW.error_code IS NULL OR NEW.error_code NOT IN ('monobank_unconfigured','monobank_unavailable','monobank_not_found','monobank_proof_invalid','monobank_profile_mismatch','monobank_pending','monobank_reversed') THEN RAISE EXCEPTION 'Inspection errors cannot imply no money';END IF;RETURN NEW;
 END IF;
 FOREACH k IN ARRAY ARRAY['profile_id','merchant_id','authority','transport','invoice_id','reference','status','created_at','modified_at','observed_at'] LOOP
  IF jsonb_typeof(p->k) IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Inspection must preserve typed string identities';END IF;
 END LOOP;
 IF NEW.error_code IS NOT NULL OR p->'verified' IS DISTINCT FROM 'true'::jsonb OR p->>'profile_id' IS DISTINCT FROM b.profile_id OR p->>'merchant_id' IS DISTINCT FROM b.profile->>'merchant_id'
 OR p->>'authority' IS DISTINCT FROM b.authority OR p->>'transport' IS DISTINCT FROM b.transport OR p->>'invoice_id' IS DISTINCT FROM NEW.invoice_id
 OR p->>'proof_hash' IS DISTINCT FROM shop_resolution_hash(p-'proof_hash') OR jsonb_typeof(p->'has_cancellations') IS DISTINCT FROM 'boolean'
 OR jsonb_typeof(p->'reference') IS DISTINCT FROM 'string' OR COALESCE(btrim(p->>'reference'),'')='' OR length(p->>'reference')>200
 OR p->>'status' IS NULL OR p->>'status' NOT IN ('created','processing','hold','success','failure','expired','reversed')
 OR jsonb_typeof(p->'amount_minor') IS DISTINCT FROM 'number' OR COALESCE(p->>'amount_minor','') !~ '^[1-9][0-9]*$'
 OR jsonb_typeof(p->'currency_code') IS DISTINCT FROM 'number' OR COALESCE(p->>'currency_code','') !~ '^[1-9][0-9]{0,2}$'
 OR p->>'currency' IS DISTINCT FROM (CASE WHEN p->>'currency_code'='980' THEN 'UAH' ELSE NULL END)
 OR (p->'final_amount_minor' IS DISTINCT FROM 'null'::jsonb AND (jsonb_typeof(p->'final_amount_minor') IS DISTINCT FROM 'number' OR COALESCE(p->>'final_amount_minor','') !~ '^(0|[1-9][0-9]*)$'))
 OR p->>'status'='success' AND p->>'final_amount_minor' IS NULL OR p->>'created_at' IS NULL OR p->>'modified_at' IS NULL OR p->>'observed_at' IS NULL
 OR (p->>'observed_at')::timestamptz<now()-interval '10 minutes' OR (p->>'observed_at')::timestamptz>now()+interval '30 seconds'
 OR (p->>'created_at')::timestamptz>(p->>'modified_at')::timestamptz OR (p->>'modified_at')::timestamptz>(p->>'observed_at')::timestamptz+interval '30 seconds'
 OR EXISTS(SELECT 1 FROM shop_payment_resolution_observations o WHERE o.case_id=c.id AND o.invoice_id=NEW.invoice_id AND o.proof->>'modified_at' IS NOT NULL
   AND ((o.proof->>'modified_at')::timestamptz>(p->>'modified_at')::timestamptz OR (o.proof->>'modified_at')::timestamptz=(p->>'modified_at')::timestamptz AND o.proof->>'status' IS DISTINCT FROM p->>'status'))
 THEN RAISE EXCEPTION 'Inspection must preserve exact fresh typed authenticated provenance';END IF;RETURN NEW;
END $$;
CREATE TRIGGER validate_shop_resolution_observation BEFORE INSERT ON shop_payment_resolution_observations FOR EACH ROW EXECUTE FUNCTION validate_shop_resolution_observation();
CREATE FUNCTION guard_shop_invoice_identity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pid text;inv text;oid int;origin text;
BEGIN
 IF TG_TABLE_NAME='shop_payment_intents' THEN
  IF NEW.invoice_id IS NULL THEN RETURN NEW;END IF;
  pid=NEW.profile_id;inv=NEW.invoice_id;origin=NEW.id;oid=NEW.order_id;
 ELSE pid=NEW.profile_id;inv=NEW.replacement_invoice_id;origin=NEW.intent_id;SELECT order_id INTO oid FROM shop_payment_intents WHERE id=origin;END IF;
 PERFORM 1 FROM orders WHERE id=oid FOR UPDATE;
 PERFORM pg_advisory_xact_lock(hashtextextended('shop-invoice:'||pid||':'||inv,0));
 IF EXISTS(SELECT 1 FROM shop_payment_intents WHERE profile_id=pid AND invoice_id=inv AND id!=origin)
 OR EXISTS(SELECT 1 FROM shop_payment_invoice_corrections WHERE profile_id=pid AND (replacement_invoice_id=inv OR original_invoice_id=inv) AND intent_id!=origin)
 THEN RAISE EXCEPTION 'Invoice identity belongs to another immutable intent';END IF;RETURN NEW;
END $$;
CREATE TRIGGER guard_shop_invoice_identity BEFORE INSERT OR UPDATE ON shop_payment_intents FOR EACH ROW EXECUTE FUNCTION guard_shop_invoice_identity();
CREATE TRIGGER guard_shop_invoice_identity BEFORE INSERT ON shop_payment_invoice_corrections FOR EACH ROW EXECUTE FUNCTION guard_shop_invoice_identity();
CREATE FUNCTION validate_shop_invoice_correction() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c shop_payment_resolution_cases%ROWTYPE;b shop_payment_intents%ROWTYPE;oldp jsonb;newp jsonb;
BEGIN
 SELECT * INTO c FROM shop_payment_resolution_cases WHERE id=NEW.case_id FOR UPDATE;SELECT * INTO b FROM shop_payment_intents WHERE id=NEW.intent_id;
 SELECT proof::jsonb INTO oldp FROM shop_payment_resolution_observations WHERE id=NEW.foreign_observation_id AND case_id=c.id AND invoice_id=b.invoice_id;
 SELECT proof::jsonb INTO newp FROM shop_payment_resolution_observations WHERE id=NEW.replacement_observation_id AND case_id=c.id AND invoice_id=NEW.replacement_invoice_id;
 IF c.kind IS DISTINCT FROM 'invoice_review' OR c.intent_id IS DISTINCT FROM b.id OR NEW.profile_id IS DISTINCT FROM b.profile_id OR NEW.original_invoice_id IS DISTINCT FROM b.invoice_id
 OR oldp IS NULL OR newp IS NULL OR oldp->'verified' IS DISTINCT FROM 'true'::jsonb OR newp->'verified' IS DISTINCT FROM 'true'::jsonb
 OR shop_resolution_reference(oldp->>'reference') IS NOT DISTINCT FROM b.reference OR newp->>'reference' IS DISTINCT FROM b.reference
 OR NEW.original_invoice_id=NEW.replacement_invoice_id OR newp->>'currency' IS DISTINCT FROM 'UAH' OR newp->>'amount_minor' IS DISTINCT FROM b.amount_minor::text
 OR newp->'has_cancellations' IS DISTINCT FROM 'false'::jsonb OR (newp->>'created_at')::timestamptz<b.created_at-interval '30 seconds'
 OR (newp->>'observed_at')::timestamptz<now()-interval '10 minutes' OR (oldp->>'observed_at')::timestamptz<now()-interval '10 minutes'
 OR EXISTS(SELECT 1 FROM shop_payment_resolution_observations WHERE case_id=c.id AND invoice_id=NEW.original_invoice_id AND shop_resolution_reference(proof->>'reference')=b.reference)
 OR EXISTS(SELECT 1 FROM shop_payment_resolution_observations WHERE case_id=c.id AND ((invoice_id=NEW.original_invoice_id AND id>NEW.foreign_observation_id) OR (invoice_id=NEW.replacement_invoice_id AND id>NEW.replacement_observation_id)))
 OR EXISTS(SELECT 1 FROM order_payments WHERE order_id=c.order_id) OR EXISTS(SELECT 1 FROM shop_payment_bank_closures WHERE case_id=c.id)
 OR EXISTS(SELECT 1 FROM shop_payment_financial_exceptions WHERE case_id=c.id)
 OR NEW.snapshot_hash IS DISTINCT FROM shop_resolution_hash(NEW.snapshot::jsonb)
 OR NEW.snapshot::jsonb IS DISTINCT FROM jsonb_build_object('intent_id',b.id,'original_invoice_id',NEW.original_invoice_id,'replacement_invoice_id',NEW.replacement_invoice_id,'foreign_observation_id',NEW.foreign_observation_id,'replacement_observation_id',NEW.replacement_observation_id,'reference',b.reference,'profile_hash',c.profile_hash)
 THEN RAISE EXCEPTION 'Correction requires positive foreign proof and exact replacement without changing original intent';END IF;RETURN NEW;
END $$;
CREATE TRIGGER validate_shop_invoice_correction BEFORE INSERT ON shop_payment_invoice_corrections FOR EACH ROW EXECUTE FUNCTION validate_shop_invoice_correction();
''')
    execute_sql('''
CREATE FUNCTION validate_shop_bank_closure() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c shop_payment_resolution_cases%ROWTYPE;b shop_payment_intents%ROWTYPE;e jsonb;p jsonb;ids jsonb;k text;
BEGIN
 SELECT * INTO c FROM shop_payment_resolution_cases WHERE id=NEW.case_id FOR UPDATE;SELECT * INTO b FROM shop_payment_intents WHERE id=c.intent_id;e=NEW.snapshot::jsonb;p=NEW.policy::jsonb;
 PERFORM 1 FROM shop_checkbox_registers WHERE id=c.register_id FOR UPDATE;
 SELECT COALESCE(jsonb_agg(inv ORDER BY inv),'[]'::jsonb) INTO ids FROM (
 SELECT b.invoice_id inv WHERE b.invoice_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM shop_payment_invoice_corrections WHERE intent_id=b.id)
 AND NOT EXISTS(SELECT 1 FROM shop_payment_resolution_observations o WHERE o.case_id=c.id AND o.invoice_id=b.invoice_id AND o.proof::jsonb->'verified'='true'::jsonb AND shop_resolution_reference(o.proof->>'reference') IS DISTINCT FROM b.reference
 AND NOT EXISTS(SELECT 1 FROM shop_payment_resolution_observations newer WHERE newer.case_id=c.id AND newer.invoice_id=b.invoice_id AND newer.id>o.id)
 AND NOT EXISTS(SELECT 1 FROM shop_payment_resolution_observations ours WHERE ours.case_id=c.id AND ours.invoice_id=b.invoice_id AND shop_resolution_reference(ours.proof->>'reference')=b.reference))
 UNION SELECT o.invoice_id FROM shop_payment_resolution_observations o WHERE o.case_id=c.id AND shop_resolution_reference(o.proof->>'reference')=b.reference
 UNION SELECT replacement_invoice_id FROM shop_payment_invoice_corrections WHERE intent_id=b.id) q;
 FOREACH k IN ARRAY ARRAY['type','source','issuer','account','document_id','line_id','intent_id','reference','verified_at'] LOOP
  IF jsonb_typeof(e->k) IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Bank closure requires typed string identities';END IF;
 END LOOP;
 FOREACH k IN ARRAY ARRAY['id','environment','issuer','account'] LOOP
  IF jsonb_typeof(p->k) IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Bank policy requires typed string identities';END IF;
 END LOOP;
 IF c.kind IS DISTINCT FROM 'invoice_review' OR jsonb_typeof(p) IS DISTINCT FROM 'object' OR p->'approved' IS DISTINCT FROM 'true'::jsonb
 OR COALESCE(p->>'id','') !~ '^[A-Za-z0-9_-]{1,64}$' OR p->>'environment' IS NULL OR p->>'environment' NOT IN ('test','production')
 OR COALESCE(p->>'issuer','') !~ '^[A-Za-z0-9_.:/-]{1,100}$' OR COALESCE(p->>'account','') !~ '^[A-Za-z0-9_.:/-]{1,100}$'
 OR jsonb_typeof(p->'document_types') IS DISTINCT FROM 'array' OR jsonb_array_length(p->'document_types')=0
 OR NEW.policy_hash IS DISTINCT FROM shop_resolution_hash(p) OR NEW.snapshot_hash IS DISTINCT FROM shop_resolution_hash(e)
 OR e->'verified' IS DISTINCT FROM 'true'::jsonb OR e->'no_money' IS DISTINCT FROM 'true'::jsonb OR e->'no_hold' IS DISTINCT FROM 'true'::jsonb
 OR e->'all_reference_invoices_closed' IS DISTINCT FROM 'true'::jsonb OR e->>'issuer' IS DISTINCT FROM p->>'issuer' OR e->>'account' IS DISTINCT FROM p->>'account'
 OR NOT COALESCE((p->'document_types') ? (e->>'type'),false) OR COALESCE(btrim(e->>'source'),'')=''
 OR COALESCE(e->>'document_id','') !~ '^[A-Za-z0-9_.:/-]{1,100}$' OR COALESCE(e->>'line_id','') !~ '^[A-Za-z0-9_.:/-]{1,100}$'
 OR jsonb_typeof(e->'order_id') IS DISTINCT FROM 'number' OR e->>'order_id' IS DISTINCT FROM c.order_id::text
 OR e->>'intent_id' IS DISTINCT FROM b.id OR e->>'reference' IS DISTINCT FROM b.reference OR e->'known_invoice_ids' IS DISTINCT FROM ids
 OR e->>'verified_at' IS NULL OR (e->>'verified_at')::timestamptz<b.created_at OR (e->>'verified_at')::timestamptz>now()+interval '30 seconds'
 OR EXISTS(SELECT 1 FROM shop_payment_resolution_observations WHERE case_id=c.id AND created_at>(e->>'verified_at')::timestamptz)
 OR EXISTS(SELECT 1 FROM shop_payment_resolution_observations o WHERE o.case_id=c.id AND shop_resolution_reference(o.proof->>'reference')=b.reference AND
 (o.proof->>'status'='success' OR o.proof::jsonb->'has_cancellations'='true'::jsonb OR COALESCE((o.proof->>'final_amount_minor')::bigint,0)>0 OR o.proof->>'amount_minor' IS DISTINCT FROM b.amount_minor::text OR o.proof->>'currency' IS DISTINCT FROM 'UAH'))
 OR EXISTS(SELECT 1 FROM shop_payment_financial_exceptions WHERE case_id=c.id) OR EXISTS(SELECT 1 FROM order_payments WHERE order_id=c.order_id)
 OR EXISTS(SELECT 1 FROM shop_checkbox_documents WHERE order_id=c.order_id) OR NOT shop_resolution_no_handover(c.order_id)
 OR NEW.identity IS DISTINCT FROM shop_resolution_hash(jsonb_build_array(translate(e->>'issuer','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),translate(e->>'account','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),translate(e->>'document_id','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),translate(e->>'line_id','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')))
 THEN RAISE EXCEPTION 'Bank closure requires exact typed no-money no-hold all-reference-invoices-closed documentary proof';END IF;RETURN NEW;
END $$;
CREATE TRIGGER validate_shop_bank_closure BEFORE INSERT ON shop_payment_bank_closures FOR EACH ROW EXECUTE FUNCTION validate_shop_bank_closure();
CREATE FUNCTION validate_shop_resolution_finalization() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c shop_payment_resolution_cases%ROWTYPE;l jsonb;i record;released bigint;
BEGIN
 SELECT * INTO c FROM shop_payment_resolution_cases WHERE id=NEW.case_id FOR UPDATE;
 IF c.order_id IS DISTINCT FROM NEW.order_id OR NOT shop_resolution_can_close(NEW.order_id)
 OR NEW.snapshot_hash IS DISTINCT FROM shop_resolution_hash(NEW.snapshot::jsonb) OR NEW.snapshot->>'case_id' IS DISTINCT FROM c.id::text
 OR NEW.snapshot->>'order_id' IS DISTINCT FROM c.order_id::text OR NEW.snapshot->>'kind' IS DISTINCT FROM c.kind OR NEW.snapshot->>'case_hash' IS DISTINCT FROM c.snapshot_hash
 OR NEW.snapshot::jsonb->'test_context' IS DISTINCT FROM to_jsonb(shop_resolution_test(c.order_id))
 OR NOT EXISTS(SELECT 1 FROM orders WHERE id=c.order_id AND status::text='cancelled')
 OR jsonb_typeof(NEW.snapshot::jsonb->'reserve_release') IS DISTINCT FROM 'array'
 OR jsonb_array_length(NEW.snapshot::jsonb->'reserve_release') IS DISTINCT FROM (SELECT count(*)::int FROM order_items WHERE order_id=c.order_id AND quantity_from_stock+quantity_received_for_order>0)
 OR (SELECT count(DISTINCT entry->>'order_item_id') FROM jsonb_array_elements(NEW.snapshot::jsonb->'reserve_release') entry) IS DISTINCT FROM jsonb_array_length(NEW.snapshot::jsonb->'reserve_release')
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.snapshot::jsonb->'reserve_release') entry LEFT JOIN order_items item_row ON item_row.id=(entry->>'order_item_id')::int
  WHERE jsonb_typeof(entry) IS DISTINCT FROM 'object' OR entry-ARRAY['order_item_id','product_id','quantity','reference_key']!='{}'::jsonb
   OR jsonb_typeof(entry->'order_item_id') IS DISTINCT FROM 'number' OR jsonb_typeof(entry->'product_id') IS DISTINCT FROM 'number' OR jsonb_typeof(entry->'quantity') IS DISTINCT FROM 'number'
   OR jsonb_typeof(entry->'reference_key') IS DISTINCT FROM 'string' OR item_row.id IS NULL OR item_row.order_id IS DISTINCT FROM c.order_id OR item_row.product_id::text IS DISTINCT FROM entry->>'product_id'
   OR item_row.quantity_from_stock+item_row.quantity_received_for_order<=0 OR (item_row.quantity_from_stock+item_row.quantity_received_for_order)::text IS DISTINCT FROM entry->>'quantity'
   OR entry->>'reference_key' IS DISTINCT FROM 'shop-resolution:'||c.id::text||':item:'||item_row.id::text||':reserve-release')
 OR EXISTS(SELECT 1 FROM shop_payment_financial_exceptions WHERE case_id=c.id)
 THEN RAISE EXCEPTION 'Finalization requires exact fulfilled refund/fiscal or documentary obligations';END IF;
 IF c.kind='paid_cancellation' AND NOT EXISTS(SELECT 1 FROM shop_checkbox_returns q JOIN shop_checkbox_documents d ON d.return_id=q.id
 WHERE q.id=(NEW.snapshot->>'financial_return_id')::int AND q.cancellation_case_id=c.id AND d.id=(NEW.snapshot->>'return_document_id')::int AND d.kind='return')
 THEN RAISE EXCEPTION 'Finalization must identify its exact full cancellation return cheque';END IF;
 FOR i IN SELECT * FROM order_items WHERE order_id=c.order_id LOOP
  SELECT value INTO l FROM jsonb_array_elements(NEW.snapshot::jsonb->'reserve_release') WHERE value->>'order_item_id'=i.id::text;
  SELECT COALESCE(sum(reserved_delta),0) INTO released FROM inventory_movements WHERE order_id=c.order_id AND order_item_id=i.id AND product_id=i.product_id;
  IF released!=0 OR i.quantity_from_stock+i.quantity_received_for_order>0 AND (l IS NULL OR jsonb_typeof(l->'quantity') IS DISTINCT FROM 'number'
   OR l->>'product_id' IS DISTINCT FROM i.product_id::text OR (l->>'quantity')::int IS DISTINCT FROM i.quantity_from_stock+i.quantity_received_for_order
   OR l->>'reference_key' IS DISTINCT FROM 'shop-resolution:'||c.id::text||':item:'||i.id::text||':reserve-release'
   OR (SELECT count(*) FROM inventory_movements m WHERE m.reference_key=l->>'reference_key' AND m.order_id=c.order_id AND m.order_item_id=i.id
      AND m.product_id=i.product_id AND m.movement_type::text='reservation_release' AND m.quantity=(l->>'quantity')::int AND m.on_hand_delta=0 AND m.reserved_delta=-m.quantity AND m.admin_user_id=NEW.operator_id)!=1)
  THEN RAISE EXCEPTION 'Finalization must release every owned reservation exactly once without stock receipt';END IF;
 END LOOP;RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER validate_shop_resolution_finalization AFTER INSERT ON shop_payment_resolution_finalizations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_shop_resolution_finalization();
CREATE FUNCTION validate_shop_financial_exception() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o shop_payment_resolution_observations%ROWTYPE;c shop_payment_resolution_cases%ROWTYPE;b shop_payment_intents%ROWTYPE;
BEGIN
 SELECT * INTO o FROM shop_payment_resolution_observations WHERE id=NEW.observation_id;SELECT * INTO c FROM shop_payment_resolution_cases WHERE id=NEW.case_id FOR UPDATE;
 SELECT * INTO b FROM shop_payment_intents WHERE id=c.intent_id;
 IF o.case_id IS DISTINCT FROM c.id OR shop_resolution_reference(o.proof->>'reference') IS DISTINCT FROM b.reference OR NEW.amount_minor<=0
 OR NEW.currency IS DISTINCT FROM o.proof->>'currency' OR NEW.amount_minor IS DISTINCT FROM COALESCE(NULLIF((o.proof->>'final_amount_minor')::bigint,0),(o.proof->>'amount_minor')::bigint)
 OR NEW.reason NOT IN ('late_success','contradictory_bank_money','surplus_payment','hold_after_closure')
 THEN RAISE EXCEPTION 'Financial exception must preserve exact known bank liability';END IF;RETURN NEW;
END $$;
CREATE TRIGGER validate_shop_financial_exception BEFORE INSERT ON shop_payment_financial_exceptions FOR EACH ROW EXECUTE FUNCTION validate_shop_financial_exception();
CREATE FUNCTION require_shop_late_exception() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c shop_payment_resolution_cases%ROWTYPE;b shop_payment_intents%ROWTYPE;p jsonb;
BEGIN
 SELECT * INTO c FROM shop_payment_resolution_cases WHERE id=NEW.case_id;SELECT * INTO b FROM shop_payment_intents WHERE id=c.intent_id;p=NEW.proof::jsonb;
 IF shop_resolution_reference(p->>'reference')=b.reference AND (p->'has_cancellations'='true'::jsonb OR p->>'amount_minor' IS DISTINCT FROM b.amount_minor::text OR p->>'currency' IS DISTINCT FROM 'UAH'
 OR p->>'status'='reversed' OR (p->>'status'='hold' AND EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations WHERE case_id=c.id)) OR p->>'status'='success' AND ((EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations WHERE case_id=c.id) AND NOT EXISTS(SELECT 1 FROM shop_payment_evidence WHERE order_id=c.order_id AND proof->>'invoice_id'=NEW.invoice_id)) OR EXISTS(SELECT 1 FROM shop_payment_evidence WHERE order_id=c.order_id AND proof->>'invoice_id' IS DISTINCT FROM NEW.invoice_id) OR p->>'final_amount_minor' IS DISTINCT FROM b.amount_minor::text))
 AND NOT EXISTS(SELECT 1 FROM shop_payment_financial_exceptions WHERE observation_id=NEW.id)
 THEN RAISE EXCEPTION 'Known contradictory or late money requires a durable financial exception';END IF;RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER require_shop_late_exception AFTER INSERT ON shop_payment_resolution_observations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_shop_late_exception();
CREATE FUNCTION protect_shop_resolution_context() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE oid int;frozen boolean;
BEGIN
 IF TG_TABLE_NAME='orders' THEN oid=OLD.id;ELSIF TG_TABLE_NAME='shop_checkbox_refund_facts' THEN SELECT order_id INTO oid FROM shop_checkbox_returns WHERE id=NEW.return_id;ELSE oid=NEW.order_id;END IF;
 IF TG_TABLE_NAME='shop_checkbox_operations' THEN
  PERFORM 1 FROM shop_checkbox_registers WHERE id=NEW.register_id FOR UPDATE;
  IF NEW.kind='return' AND NEW.status='unknown' AND OLD.status='queued' AND EXISTS(SELECT 1 FROM shop_payment_financial_exceptions e JOIN shop_payment_resolution_cases c ON c.id=e.case_id WHERE c.order_id=oid) THEN RAISE EXCEPTION 'Financial review blocks undispatched return operation';END IF;
  IF shop_resolution_test(oid) AND NEW.authority IS DISTINCT FROM 'test' THEN RAISE EXCEPTION 'Resolution test context cannot become provider fiscal truth';END IF;
  -- Existing immutable operation identity permits lookup/application after its
  -- dispatch fence; newly queued or undispatched close work remains blocked.
  IF NEW.kind IN ('z','shift_close') AND NOT (TG_OP='UPDATE' AND OLD.dispatched_at IS NOT NULL)
  AND EXISTS(SELECT 1 FROM shop_payment_resolution_cases c WHERE c.register_id=NEW.register_id AND
   (EXISTS(SELECT 1 FROM shop_payment_financial_exceptions e WHERE e.case_id=c.id) OR NOT EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations f WHERE f.case_id=c.id) AND (c.kind='paid_cancellation' OR NOT EXISTS(SELECT 1 FROM shop_payment_evidence WHERE order_id=c.order_id))))
  THEN RAISE EXCEPTION 'Unresolved payment resolutions block fiscal shift closing';END IF;RETURN NEW;
 END IF;
 IF TG_TABLE_NAME!='orders' THEN PERFORM 1 FROM orders WHERE id=oid FOR UPDATE;END IF;
 frozen=EXISTS(SELECT 1 FROM shop_payment_resolution_cases WHERE order_id=oid);
 IF TG_TABLE_NAME='orders' THEN
  IF frozen AND (NEW.total_amount IS DISTINCT FROM OLD.total_amount OR NEW.subtotal_amount IS DISTINCT FROM OLD.subtotal_amount OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount
   OR NEW.channel IS DISTINCT FROM OLD.channel OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method
   OR NEW.payment_method IS DISTINCT FROM OLD.payment_method AND NOT EXISTS(SELECT 1 FROM shop_payment_evidence WHERE order_id=oid)
   OR EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations WHERE order_id=oid) AND NEW.status::text!='cancelled')
  THEN RAISE EXCEPTION 'Resolution economics and final cancellation are immutable';END IF;
 ELSIF TG_TABLE_NAME IN ('shop_checkbox_returns','shop_checkbox_refund_facts') THEN
  IF EXISTS(SELECT 1 FROM shop_payment_financial_exceptions e JOIN shop_payment_resolution_cases c ON c.id=e.case_id WHERE c.order_id=oid) THEN RAISE EXCEPTION 'Financial exception blocks new refunds and claims';END IF;
 ELSIF TG_TABLE_NAME IN ('order_payments','shop_payment_confirmations','shop_payment_evidence') THEN
  IF EXISTS(SELECT 1 FROM shop_payment_resolution_cases c WHERE c.order_id=oid AND (c.kind='paid_cancellation' OR EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations f WHERE f.case_id=c.id) OR EXISTS(SELECT 1 FROM shop_payment_financial_exceptions e WHERE e.case_id=c.id)))
  THEN RAISE EXCEPTION 'Closed or cancellation resolution cannot admit new money';END IF;
 ELSIF frozen AND (shop_resolution_active(oid) OR EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations WHERE order_id=oid)) THEN RAISE EXCEPTION 'Resolution order cannot admit handover, completion or alternate workflow';END IF;RETURN NEW;
END $$;
CREATE TRIGGER protect_shop_resolution_context BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION protect_shop_resolution_context();
CREATE TRIGGER protect_shop_resolution_context BEFORE INSERT OR UPDATE ON shop_checkbox_operations FOR EACH ROW EXECUTE FUNCTION protect_shop_resolution_context();
CREATE FUNCTION protect_shop_resolution_stock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE oid int;c shop_payment_resolution_cases%ROWTYPE;
BEGIN
 SELECT order_id INTO oid FROM order_items WHERE id=NEW.order_item_id;oid=COALESCE(oid,NEW.order_id);
 SELECT * INTO c FROM shop_payment_resolution_cases WHERE (order_id=oid OR order_id=NEW.order_id) AND (shop_resolution_active(order_id) OR EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations WHERE order_id=shop_payment_resolution_cases.order_id)) ORDER BY id DESC LIMIT 1;
 IF c.id IS NULL THEN RETURN NEW;END IF;
 IF NOT EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations f,jsonb_array_elements(f.snapshot::jsonb->'reserve_release') l
 WHERE f.case_id=c.id AND EXISTS(SELECT 1 FROM order_items i WHERE i.id=NEW.order_item_id AND i.order_id=c.order_id AND i.product_id=NEW.product_id)
 AND NEW.order_id=c.order_id AND NEW.order_item_id::text=l->>'order_item_id' AND NEW.product_id::text=l->>'product_id'
 AND NEW.reference_key=l->>'reference_key' AND NEW.movement_type::text='reservation_release' AND NEW.quantity::text=l->>'quantity'
 AND NEW.on_hand_delta=0 AND NEW.reserved_delta=-NEW.quantity AND NEW.admin_user_id=f.operator_id)
 THEN RAISE EXCEPTION 'Resolution inventory is frozen until its exact once-only owned reserve release';END IF;RETURN NEW;
END $$;
CREATE FUNCTION protect_shop_resolution_stock_fence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE oid int;
BEGIN
 IF NEW.movement_type::text NOT IN ('customer_return','shipment','reservation','reservation_release') THEN RETURN NEW;END IF;
 SELECT order_id INTO oid FROM order_items WHERE id=NEW.order_item_id;
 PERFORM 1 FROM orders WHERE id=oid OR id=NEW.order_id ORDER BY id FOR UPDATE;RETURN NEW;
END $$;
CREATE TRIGGER protect_shop_resolution_stock_fence BEFORE INSERT ON inventory_movements FOR EACH ROW EXECUTE FUNCTION protect_shop_resolution_stock_fence();
CREATE CONSTRAINT TRIGGER protect_shop_resolution_stock AFTER INSERT ON inventory_movements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION protect_shop_resolution_stock();
CREATE FUNCTION protect_shop_resolution_items() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a int;b int;
BEGIN
 IF TG_OP!='INSERT' THEN a=OLD.order_id;END IF;IF TG_OP!='DELETE' THEN b=NEW.order_id;END IF;
 PERFORM 1 FROM orders WHERE id IN(a,b) ORDER BY id FOR UPDATE;
 IF EXISTS(SELECT 1 FROM shop_payment_resolution_cases WHERE order_id IN(a,b)) AND (TG_OP!='UPDATE' OR NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.product_id IS DISTINCT FROM OLD.product_id
 OR NEW.quantity IS DISTINCT FROM OLD.quantity OR NEW.price IS DISTINCT FROM OLD.price OR NEW.total_price IS DISTINCT FROM OLD.total_price OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount
 OR shop_resolution_active(COALESCE(a,b)) AND (NEW.quantity_from_stock IS DISTINCT FROM OLD.quantity_from_stock OR NEW.quantity_received_for_order IS DISTINCT FROM OLD.quantity_received_for_order))
 THEN RAISE EXCEPTION 'Resolution goods and reserve ownership are immutable';END IF;
 IF TG_OP='DELETE' THEN RETURN OLD;ELSE RETURN NEW;END IF;
END $$;
CREATE TRIGGER protect_shop_resolution_items BEFORE INSERT OR UPDATE OR DELETE ON order_items FOR EACH ROW EXECUTE FUNCTION protect_shop_resolution_items();
''')
    for table in ('shop_payment_resolution_observations','shop_payment_invoice_corrections','shop_payment_bank_closures','shop_payment_resolution_finalizations','shop_payment_financial_exceptions'):
        op.execute(f'CREATE TRIGGER immutable_shop_resolution BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')
    for table in ('order_payments','shop_payment_confirmations','shop_payment_evidence','shop_checkbox_returns','shop_checkbox_refund_facts','order_handovers','shop_fulfillment_facts','shop_completions','shop_payment_intents','shop_carrier_returns','shop_np_return_cases','shop_fiscal_documents','shop_financial_returns','fiscal_documents','qr_payment_attempts'):
        op.execute(f'CREATE TRIGGER protect_shop_resolution_context BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION protect_shop_resolution_context()')
    # Add a narrow direct cancellation basis while preserving pickup and NP bases.
    replace('protect_shop_checkbox_return','JOIN shop_fulfillment_facts h ON h.order_id=s.id','LEFT JOIN shop_fulfillment_facts h ON h.order_id=s.id')
    value=op.get_bind().execute(sa.text("SELECT pg_get_functiondef(to_regprocedure('protect_shop_checkbox_return()'))")).scalar()
    start=value.index("((h.kind='pickup'");end=value.index(" AND r.shift_status='open'",start)
    old=value[start:end]
    r4="""EXISTS(SELECT 1 FROM shop_payment_resolution_cases rc WHERE rc.id=NEW.cancellation_case_id AND rc.order_id=NEW.order_id AND rc.kind='paid_cancellation'
      AND rc.register_id=NEW.register_id AND rc.profile_hash=r.profile_hash AND rc.payment_confirmation_id=d.payment_confirmation_id AND NOT EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations WHERE case_id=rc.id)
      AND shop_resolution_no_handover(NEW.order_id) AND NEW.carrier_receipt_id IS NULL AND NEW.lines::jsonb=o.snapshot::jsonb->'items'
      AND NEW.amount_minor=(o.snapshot->>'amount_minor')::bigint AND NEW.amount_minor=(rc.snapshot->>'amount_minor')::bigint AND (NOT shop_resolution_test(NEW.order_id) OR r.authority='test'))"""
    replace('protect_shop_checkbox_return',old,'((NEW.cancellation_case_id IS NULL AND '+old+') OR ('+r4+'))')
    replace('validate_shop_checkbox_document','OR (r.carrier_receipt_id IS NOT NULL AND nr.id IS NOT NULL AND nc.id IS NOT NULL))',
        """OR (r.carrier_receipt_id IS NOT NULL AND nr.id IS NOT NULL AND nc.id IS NOT NULL)
         OR (r.cancellation_case_id IS NOT NULL AND r.carrier_receipt_id IS NULL AND EXISTS(SELECT 1 FROM shop_payment_resolution_cases rc
          WHERE rc.id=r.cancellation_case_id AND rc.order_id=r.order_id AND rc.kind='paid_cancellation' AND rc.register_id=r.register_id AND shop_resolution_no_handover(r.order_id))))""")
    replace('validate_shop_checkbox_physical','IF q.carrier_receipt_id IS NOT NULL THEN',"IF q.cancellation_case_id IS NOT NULL THEN RAISE EXCEPTION 'Pre-handover cancellation cannot fabricate physical receipt';END IF; IF q.carrier_receipt_id IS NOT NULL THEN")
    replace('protect_shop_order',"AND NOT EXISTS(SELECT 1 FROM shop_np_unpaid_closures nc WHERE nc.order_id=OLD.id AND shop_np_unpaid_can_close(OLD.id,nc.case_id))",
        "AND NOT EXISTS(SELECT 1 FROM shop_np_unpaid_closures nc WHERE nc.order_id=OLD.id AND shop_np_unpaid_can_close(OLD.id,nc.case_id)) AND NOT shop_resolution_can_close(OLD.id)")
    replace('protect_shop_r2_order',"OR NEW.status::text='cancelled' AND OLD.status::text!='cancelled'","OR NEW.status::text='cancelled' AND OLD.status::text!='cancelled' AND NOT shop_resolution_can_close(OLD.id)")
    # Preserve the immutable original candidate; only accepted correction lineage can
    # supply the effective invoice used by the existing strict R2 proof validator.
    replace('validate_shop_payment_evidence','s order_shipments%ROWTYPE;','s order_shipments%ROWTYPE; effective text; correction shop_payment_invoice_corrections%ROWTYPE;')
    replace('validate_shop_payment_evidence','SELECT * INTO p FROM order_payments WHERE id=NEW.payment_id;',
        "SELECT * INTO p FROM order_payments WHERE id=NEW.payment_id; SELECT * INTO correction FROM shop_payment_invoice_corrections WHERE intent_id=b.id; effective=COALESCE(correction.replacement_invoice_id,b.invoice_id); IF NEW.correction_id IS DISTINCT FROM correction.id THEN RAISE EXCEPTION 'Exact effective invoice correction lineage required';END IF;")
    for old,new in [("NEW.proof::jsonb->>'invoice_id' IS DISTINCT FROM b.invoice_id","NEW.proof::jsonb->>'invoice_id' IS DISTINCT FROM effective"),('jsonb_build_array(b.profile_id,b.invoice_id)','jsonb_build_array(b.profile_id,effective)'),('p.evidence IS DISTINCT FROM b.invoice_id','p.evidence IS DISTINCT FROM effective')]:
        replace('validate_shop_payment_evidence',old,new)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM shop_payment_resolution_cases)')).scalar():
        raise RuntimeError('R4 immutable reconciliation history exists; downgrade refused')
    definitions=op.get_bind().execute(sa.text('SELECT definition FROM shop_resolution_migration_functions')).scalars().all()
    for definition in definitions:execute_sql(definition)
    for table in ('orders','shop_checkbox_operations','order_payments','shop_payment_confirmations','shop_payment_evidence','shop_checkbox_returns','shop_checkbox_refund_facts','order_handovers','shop_fulfillment_facts','shop_completions','shop_payment_intents','shop_carrier_returns','shop_np_return_cases','shop_fiscal_documents','shop_financial_returns','fiscal_documents','qr_payment_attempts'):
        op.execute(f'DROP TRIGGER protect_shop_resolution_context ON {table}')
    op.execute('DROP TRIGGER guard_shop_invoice_identity ON shop_payment_intents')
    op.execute('DROP TRIGGER protect_shop_resolution_stock ON inventory_movements')
    op.execute('DROP TRIGGER protect_shop_resolution_stock_fence ON inventory_movements')
    op.execute('DROP TRIGGER protect_shop_resolution_items ON order_items')
    op.execute('ALTER TABLE shop_checkbox_returns DROP COLUMN cancellation_case_id')
    op.execute('ALTER TABLE shop_payment_evidence DROP COLUMN correction_id')
    for table in ('shop_payment_financial_exceptions','shop_payment_resolution_finalizations','shop_payment_bank_closures','shop_payment_invoice_corrections','shop_payment_resolution_observations','shop_payment_resolution_cases'):
        op.execute('DROP TABLE '+table+' CASCADE')
    for name in ('protect_shop_resolution_stock_fence','protect_shop_resolution_items','protect_shop_resolution_stock','protect_shop_resolution_context','require_shop_late_exception','validate_shop_financial_exception','validate_shop_resolution_finalization','validate_shop_bank_closure','validate_shop_invoice_correction','guard_shop_invoice_identity','validate_shop_resolution_observation','protect_shop_resolution_case'):
        op.execute('DROP FUNCTION IF EXISTS '+name+'() CASCADE')
    for name in ('shop_resolution_can_close(int)','shop_resolution_no_handover(int)','shop_resolution_active(int)','shop_resolution_test(int)','shop_resolution_reference(text)','shop_resolution_hash(jsonb)'):
        op.execute('DROP FUNCTION IF EXISTS '+name+' CASCADE')
    op.drop_table('shop_resolution_migration_functions')
