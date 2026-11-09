"""Keep exact bank-selected POS URLs only with the original dispatch proof."""

from alembic import op
import sqlalchemy as sa


revision = "0151_master_mono_url_provenance"
down_revision = "0150_master_mono_expired_close"
branch_labels = None
depends_on = None

_SOURCE = "mono_authenticated_create_v1"
_LEGACY_START = "SELECT COALESCE("
_PROVIDER_START = f"""SELECT CASE WHEN proof ? 'payment_url_source' THEN COALESCE(
            proof->>'payment_url_source' = '{_SOURCE}'
            AND proof->>'authority' = 'provider' AND proof->>'transport' = 'https'
            AND proof->'returned_page_verified' = 'true'::jsonb AND proof->>'page_url' = url
            AND proof->'currency_code' = '980'::jsonb
            AND proof->>'status' = 'created' AND proof->'has_cancellations' = 'false'::jsonb
            AND jsonb_typeof(proof->'created_at') = 'string'
            AND jsonb_typeof(proof->'observed_at') = 'string'
            AND (proof->>'created_at')::timestamptz <= (proof->>'observed_at')::timestamptz
            AND master_mono_bank_selected_url_valid(url), false)
        ELSE COALESCE("""
_LEGACY_END = "), false\n        )\n        FROM ("
_PROVIDER_END = "), false\n        ) END\n        FROM ("
_BINDING = "OR NOT master_mono_returned_url_valid(NEW.invoice_id, NEW.page_url, NEW.create_proof::jsonb)"
_DISPATCH = _BINDING + f"""
          OR NOT master_mono_candidate_binding_valid(q.id, NEW.invoice_id, NEW.page_url, NEW.create_proof::jsonb)
          OR (q.authority = 'provider' AND NEW.page_url IS NOT NULL
              AND NEW.create_proof::jsonb->>'payment_url_source' IS DISTINCT FROM '{_SOURCE}')
          OR (NEW.create_proof::jsonb->>'payment_url_source' = '{_SOURCE}' AND (
              NEW.create_proof::jsonb->'dispatch_request_id' IS DISTINCT FROM to_jsonb(q.id)
              OR (NEW.create_proof::jsonb->>'observed_at')::timestamptz NOT BETWEEN
                  clock_timestamp()-interval '30 seconds' AND clock_timestamp()+interval '30 seconds'
          ))"""


_CLOSE_START = "AND (SELECT array_agg(DISTINCT i.response_json"
_CLOSE_CANDIDATE = """AND NOT EXISTS (SELECT 1 FROM master_mono_candidate_conflicts WHERE request_id=q.id)
        AND (CASE WHEN EXISTS (SELECT 1 FROM master_mono_create_candidates WHERE request_id=q.id)
          THEN (SELECT ARRAY[c.invoice_id::text] FROM master_mono_create_candidates c WHERE c.request_id=q.id
            AND (p->>'created_at')::timestamptz <= c.captured_at+interval '30 seconds')
          ELSE (SELECT array_agg(DISTINCT i.response_json"""
_CLOSE_END = ")=ARRAY[p->>'invoice_id']"
_CLOSE_CANDIDATE_END = ") END)=ARRAY[p->>'invoice_id']"


def _guards(reverse=False):
    for signature, pairs in (
        ("master_mono_returned_url_valid(text,text,jsonb)", ((_LEGACY_START, _PROVIDER_START), (_LEGACY_END, _PROVIDER_END))),
        ("validate_master_mono_fact()", ((_BINDING, _DISPATCH),)),
        ("validate_master_mono_expired_closure()", (
            (_CLOSE_START, _CLOSE_CANDIDATE), (_CLOSE_END, _CLOSE_CANDIDATE_END),
        )),
    ):
        definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                                         {"signature": signature})
        for before, after in pairs:
            old, new = (after, before) if reverse else (before, after)
            if not definition or definition.count(old) != 1:
                raise RuntimeError(f"Unexpected master Mono URL provenance guard: {signature}")
            definition = definition.replace(old, new, 1)
        op.execute(definition)


def upgrade():
    op.execute(r"""
    CREATE FUNCTION master_mono_bank_selected_url_valid(url text)
    RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
        SELECT COALESCE(
            length(url) <= 240 AND length(tail) > 0 AND length(host) <= 253
            AND host COLLATE "C" ~ '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'
            AND host !~ '(^|\.)xn--'
            AND host !~ '\.(alt|arpa|corp|example|home|internal|intranet|invalid|lan|local|localdomain|localhost|onion|private|test)$'
            AND url COLLATE "C" !~ '[^!-~]|#' AND position(chr(92) in url) = 0
            AND url !~* '%(0[0-9a-f]|1[0-9a-f]|7f|5c)'
            AND position('%' in regexp_replace(url, '%[0-9a-fA-F]{2}', '', 'g')) = 0
            AND split_part(tail, '?', 1) COLLATE "C" !~ '["<>`{}^|]'
            AND (position('?' in tail) = 0 OR substring(tail from position('?' in tail) + 1) !~ $rx$["'<>]$rx$)
            AND replace(lower(split_part(tail, '?', 1)), '%2e', '.') !~ '(^|/)\.{1,2}(/|$)', false)
        FROM (SELECT substring(url from '^https://([^/]+)/') AS host,
                     regexp_replace(url, '^https://[^/]+/', '') AS tail) AS parts
    $$
    """)
    op.create_table(
        "master_mono_create_candidates",
        sa.Column("request_id", sa.Integer, sa.ForeignKey("master_mono_create_requests.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("invoice_id", sa.String(100), nullable=False, unique=True),
        sa.Column("page_url", sa.String(240), nullable=False),
        sa.Column("validity_seconds", sa.Integer, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "master_mono_candidate_conflicts",
        sa.Column("request_id", sa.Integer, sa.ForeignKey("master_mono_create_candidates.request_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "master_mono_candidate_expiries",
        sa.Column("request_id", sa.Integer, sa.ForeignKey("master_mono_create_candidates.request_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("proof", sa.JSON, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
    CREATE FUNCTION validate_master_mono_candidate() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q master_mono_create_requests%ROWTYPE; sale orders%ROWTYPE;
            c master_mono_create_candidates%ROWTYPE; p jsonb;
            conflict_keys constant text[] := ARRAY['source','reason','identity','identity_mismatches','status','amount_minor',
                'currency_code','final_amount_minor','has_cancellations','created_at','modified_at',
                'observed_at','status_proof_hash','fiscal_proof_hash','fiscal_check_count'];
    BEGIN
      SELECT * INTO q FROM master_mono_create_requests WHERE id=NEW.request_id;
      SELECT * INTO sale FROM orders WHERE id=q.order_id FOR UPDATE;
      IF q.id IS NULL OR q.authority IS DISTINCT FROM 'provider' OR q.transport IS DISTINCT FROM 'https'
        OR sale.id IS NULL OR sale.simulation OR sale.channel IS DISTINCT FROM 'backoffice_pos'
        OR sale.created_by_user_id IS DISTINCT FROM q.operator_id
        OR sale.total_amount*100 IS DISTINCT FROM q.amount_minor
        OR sale.status::text NOT IN ('pending','confirmed')
        OR EXISTS (SELECT 1 FROM master_mono_expired_closures WHERE order_id=sale.id)
        OR (TG_TABLE_NAME='master_mono_create_candidates'
            AND EXISTS (SELECT 1 FROM master_mono_bindings WHERE order_id=sale.id))
        OR (TG_TABLE_NAME<>'master_mono_candidate_conflicts'
            AND EXISTS (SELECT 1 FROM order_payments WHERE order_id=sale.id))
        OR EXISTS (SELECT 1 FROM order_handovers WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM order_shipments WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM qr_payment_attempts WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM fiscal_documents WHERE order_id=sale.id)
      THEN RAISE EXCEPTION 'Captured Mono evidence requires the original open live dispatch'; END IF;
      IF TG_TABLE_NAME = 'master_mono_create_candidates' THEN
        IF NEW.invoice_id IS NULL OR NEW.invoice_id !~ '^[A-Za-z0-9_-]{1,100}$'
          OR NOT master_mono_bank_selected_url_valid(NEW.page_url) OR NEW.validity_seconds IS NULL
          OR NEW.validity_seconds NOT BETWEEN 60 AND 86400 OR NEW.captured_at IS NULL
          OR NEW.captured_at NOT BETWEEN q.created_at AND q.created_at + interval '15 minutes'
          OR NEW.captured_at NOT BETWEEN clock_timestamp()-interval '30 seconds' AND clock_timestamp()+interval '30 seconds'
        THEN RAISE EXCEPTION 'Malformed captured Mono evidence'; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('master-mono-invoice:'||NEW.invoice_id, 151));
        IF EXISTS (SELECT 1 FROM master_mono_bindings WHERE invoice_id=NEW.invoice_id)
          OR EXISTS (SELECT 1 FROM master_mono_create_candidates
            WHERE invoice_id=NEW.invoice_id OR request_id=NEW.request_id)
        THEN RAISE EXCEPTION 'Captured Mono invoice conflicts with existing evidence'; END IF;
      ELSE
        IF NEW.observed_at NOT BETWEEN clock_timestamp()-interval '30 seconds' AND clock_timestamp()+interval '30 seconds'
        THEN RAISE EXCEPTION 'Stale Mono candidate observation'; END IF;
        IF TG_TABLE_NAME='master_mono_candidate_expiries' THEN
          SELECT * INTO c FROM master_mono_create_candidates WHERE request_id=q.id;
          p := NEW.proof::jsonb;
          IF NOT COALESCE(
            p->'verified'='true'::jsonb AND p->>'status'='expired' AND p->'has_cancellations'='false'::jsonb
            AND p->'final_amount_minor' IN ('null'::jsonb,'0'::jsonb)
            AND p->>'invoice_id'=c.invoice_id AND p->>'reference'=q.reference
            AND p->'amount_minor'=to_jsonb(q.amount_minor) AND p->'currency_code'='980'::jsonb
            AND p->>'currency'='UAH' AND p->>'merchant_id'=q.profile::jsonb->>'merchant_id'
            AND p->>'profile_id'=q.profile_id AND p->>'authority'=q.authority AND p->>'transport'=q.transport
            AND (p->>'created_at')::timestamptz BETWEEN q.created_at-interval '30 seconds' AND c.captured_at+interval '30 seconds'
            AND (p->>'observed_at')::timestamptz BETWEEN
                clock_timestamp()-interval '30 seconds' AND clock_timestamp()+interval '30 seconds'
            AND (p->>'observed_at')::timestamptz >= c.captured_at
            AND (p->>'modified_at')::timestamptz BETWEEN (p->>'created_at')::timestamptz
                AND (p->>'observed_at')::timestamptz + interval '30 seconds'
            AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex')
            AND NOT EXISTS (SELECT 1 FROM master_mono_candidate_conflicts WHERE request_id=q.id)
            AND NOT EXISTS (SELECT 1 FROM master_mono_bindings WHERE order_id=sale.id
                AND create_proof::jsonb->>'status'='success'), false)
          THEN RAISE EXCEPTION 'Mono candidate expiry requires fresh matching unpaid proof'; END IF;
        ELSE
          SELECT * INTO c FROM master_mono_create_candidates WHERE request_id=q.id;
          p := NEW.evidence::jsonb;
          IF jsonb_typeof(p) IS DISTINCT FROM 'object'
          THEN RAISE EXCEPTION 'Invalid Mono conflict evidence'; END IF;
          IF NOT COALESCE(
            p ?& conflict_keys AND p-conflict_keys='{}'::jsonb
            AND p->>'source'='mono_authenticated_status_v1'
            AND p->>'reason' IN ('identity_mismatch','cancellation_evidence','reversed','amount_conflict',
                'after_expiry','after_payment','expiry_amount_conflict','fiscal_checks_present')
            AND p->>'identity'=CASE WHEN p->>'reason'='identity_mismatch' THEN 'mismatch' ELSE 'matched' END
            AND jsonb_typeof(p->'identity_mismatches')='array'
            AND p->'identity_mismatches' <@ '["invoice_id","reference","amount","currency","merchant","profile","authority","timing","proof_integrity"]'::jsonb
            AND CASE WHEN p->>'identity'='mismatch' THEN p->'identity_mismatches'<>'[]'::jsonb
                ELSE p->'identity_mismatches'='[]'::jsonb
                  AND p->'amount_minor'=to_jsonb(q.amount_minor) AND p->'currency_code'='980'::jsonb END
            AND p->>'status' IN ('created','processing','hold','success','failure','expired','reversed')
            AND jsonb_typeof(p->'has_cancellations')='boolean'
            AND jsonb_typeof(p->'amount_minor')='number' AND p->>'amount_minor' ~ '^[1-9][0-9]*$'
            AND jsonb_typeof(p->'currency_code')='number' AND p->>'currency_code' ~ '^[1-9][0-9]{0,2}$'
            AND (p->'final_amount_minor'='null'::jsonb OR
                (jsonb_typeof(p->'final_amount_minor')='number' AND p->>'final_amount_minor' ~ '^(0|[1-9][0-9]*)$'))
            AND CASE p->>'reason'
                WHEN 'cancellation_evidence' THEN p->'has_cancellations'='true'::jsonb
                WHEN 'reversed' THEN p->>'status'='reversed'
                WHEN 'amount_conflict' THEN p->>'status'='success'
                    AND jsonb_typeof(p->'final_amount_minor')='number' AND p->'final_amount_minor'<>p->'amount_minor'
                WHEN 'after_expiry' THEN EXISTS (SELECT 1 FROM master_mono_candidate_expiries WHERE request_id=q.id)
                    AND (p->>'status'<>'expired' OR p->'has_cancellations'='true'::jsonb
                        OR p->'final_amount_minor' NOT IN ('null'::jsonb,'0'::jsonb))
                WHEN 'after_payment' THEN p->>'status'<>'success' AND (
                    EXISTS (SELECT 1 FROM order_payments WHERE order_id=sale.id)
                    OR EXISTS (SELECT 1 FROM master_mono_bindings WHERE order_id=sale.id AND create_proof::jsonb->>'status'='success'))
                WHEN 'expiry_amount_conflict' THEN p->>'status'='expired'
                    AND p->'final_amount_minor' NOT IN ('null'::jsonb,'0'::jsonb)
                ELSE true END
            AND jsonb_typeof(p->'status_proof_hash')='string' AND p->>'status_proof_hash' ~ '^[a-f0-9]{64}$'
            AND CASE WHEN p->>'reason'='fiscal_checks_present'
              THEN p->>'status'='expired' AND p->'has_cancellations'='false'::jsonb
                AND p->'final_amount_minor' IN ('null'::jsonb,'0'::jsonb)
                AND EXISTS (SELECT 1 FROM master_mono_candidate_expiries WHERE request_id=q.id)
                AND jsonb_typeof(p->'fiscal_proof_hash')='string'
                AND p->>'fiscal_proof_hash' ~ '^[a-f0-9]{64}$'
                AND jsonb_typeof(p->'fiscal_check_count')='number' AND p->>'fiscal_check_count' ~ '^([1-9][0-9]?|100)$'
              ELSE p->'fiscal_proof_hash'='null'::jsonb AND p->'fiscal_check_count'='null'::jsonb END
            AND (p->>'observed_at')::timestamptz BETWEEN
                greatest(c.captured_at-interval '30 seconds', NEW.observed_at-interval '30 seconds') AND NEW.observed_at+interval '30 seconds'
            AND (p->>'modified_at')::timestamptz BETWEEN (p->>'created_at')::timestamptz
                AND (p->>'observed_at')::timestamptz+interval '30 seconds'
            AND NEW.evidence_hash=encode(sha256(convert_to(shop_payment_canonical(p),'UTF8')),'hex'), false)
          THEN RAISE EXCEPTION 'Invalid Mono conflict evidence'; END IF;
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER validate_master_mono_create_candidate BEFORE INSERT ON master_mono_create_candidates
      FOR EACH ROW EXECUTE FUNCTION validate_master_mono_candidate();
    CREATE TRIGGER immutable_master_mono_create_candidate BEFORE UPDATE OR DELETE ON master_mono_create_candidates
      FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation();
    CREATE TRIGGER validate_master_mono_candidate_conflict BEFORE INSERT ON master_mono_candidate_conflicts
      FOR EACH ROW EXECUTE FUNCTION validate_master_mono_candidate();
    CREATE TRIGGER immutable_master_mono_candidate_conflict BEFORE UPDATE OR DELETE ON master_mono_candidate_conflicts
      FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation();
    CREATE TRIGGER validate_master_mono_candidate_expiry BEFORE INSERT ON master_mono_candidate_expiries
      FOR EACH ROW EXECUTE FUNCTION validate_master_mono_candidate();
    CREATE TRIGGER immutable_master_mono_candidate_expiry BEFORE UPDATE OR DELETE ON master_mono_candidate_expiries
      FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation();

    CREATE FUNCTION guard_master_mono_candidate_terminal() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM master_mono_create_requests q
          JOIN master_mono_create_candidates c ON c.request_id=q.id WHERE q.order_id=NEW.order_id)
      THEN RETURN NEW; END IF;
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF EXISTS (SELECT 1 FROM master_mono_create_requests q
          WHERE q.order_id=NEW.order_id AND (
            EXISTS (SELECT 1 FROM master_mono_candidate_conflicts WHERE request_id=q.id)
            OR EXISTS (SELECT 1 FROM master_mono_candidate_expiries WHERE request_id=q.id)))
      THEN RAISE EXCEPTION 'Terminal Mono candidate blocks financial or handover facts'; END IF;
      RETURN NEW;
    END $$;
    """)
    for table in ("order_payments", "master_mono_payments", "master_mono_receipts", "master_mono_artifacts", "order_handovers"):
        op.execute(f"""CREATE TRIGGER guard_master_mono_candidate_terminal BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION guard_master_mono_candidate_terminal()""")
    op.execute(f"""
    CREATE FUNCTION master_mono_candidate_binding_valid(dispatch integer, invoice text, url text, proof jsonb)
    RETURNS boolean LANGUAGE plpgsql AS $$
    DECLARE c master_mono_create_candidates%ROWTYPE;
    BEGIN
      PERFORM pg_advisory_xact_lock(hashtextextended('master-mono-invoice:'||invoice, 151));
      SELECT * INTO c FROM master_mono_create_candidates WHERE invoice_id=invoice OR request_id=dispatch
        ORDER BY (request_id=dispatch) DESC LIMIT 1;
      IF c.request_id IS NULL THEN
        RETURN proof->>'payment_url_source' IS DISTINCT FROM '{_SOURCE}';
      END IF;
      RETURN COALESCE(c.request_id=dispatch AND c.invoice_id=invoice
        AND NOT EXISTS (SELECT 1 FROM master_mono_candidate_conflicts WHERE request_id=dispatch)
        AND NOT EXISTS (SELECT 1 FROM master_mono_candidate_expiries WHERE request_id=dispatch)
        AND proof->'dispatch_request_id'=to_jsonb(dispatch)
        AND proof->'validity_seconds'=to_jsonb(c.validity_seconds)
        AND (proof->>'created_at')::timestamptz <= c.captured_at+interval '30 seconds'
        AND proof->'currency_code'='980'::jsonb
        AND proof->'has_cancellations'='false'::jsonb
        AND proof->>'status' IN ('created','processing','hold','success')
        AND (proof->>'status'<>'success' OR proof->'final_amount_minor'=(
            SELECT to_jsonb(amount_minor) FROM master_mono_create_requests WHERE id=dispatch))
        AND (proof->>'observed_at')::timestamptz BETWEEN
          clock_timestamp()-interval '30 seconds' AND clock_timestamp()+interval '30 seconds'
        AND (proof->>'observed_at')::timestamptz >= c.captured_at
        AND (CASE WHEN url IS NULL THEN
          NOT (proof ? 'payment_url_source') AND NOT (proof ? 'page_url')
          ELSE url=c.page_url AND proof->>'payment_url_source'='{_SOURCE}'
            AND clock_timestamp() < (proof->>'created_at')::timestamptz + c.validity_seconds * interval '1 second'
          END), false);
    END $$;
    """)
    _guards()


def downgrade():
    op.execute("LOCK TABLE master_mono_create_candidates, master_mono_bindings IN SHARE ROW EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM master_mono_create_candidates)")):
        raise RuntimeError("Preserve original POS create acknowledgements; provider-provenance payment links exist")
    _guards(reverse=True)
    op.execute("DROP FUNCTION master_mono_candidate_binding_valid(integer,text,text,jsonb)")
    op.execute("DROP FUNCTION guard_master_mono_candidate_terminal() CASCADE")
    op.drop_table("master_mono_candidate_expiries")
    op.drop_table("master_mono_candidate_conflicts")
    op.drop_table("master_mono_create_candidates")
    op.execute("DROP FUNCTION validate_master_mono_candidate()")
    op.execute("DROP FUNCTION master_mono_bank_selected_url_valid(text)")
