"""Allow documentary review of an unbound durable Shop invoice attempt."""
from alembic import op
import sqlalchemy as sa

revision = "0154_unbound_shop_resolution"
down_revision = "0153_order_owner_archive"
branch_labels = None
depends_on = None

_UNBOUND = """ OR p.invoice_id IS NULL AND NOT EXISTS(
      SELECT 1 FROM shop_online_payment_requests a
      WHERE a.order_id=NEW.order_id AND a.intent_id=p.id
  )"""
_CANDIDATE = """
 UNION SELECT candidate.inv FROM (
     SELECT a.facts->>'invoice_id' AS inv FROM shop_operation_audits a
     JOIN shop_online_payment_requests r ON r.order_id=a.order_id AND r.intent_id=b.id
     WHERE a.operation='online_invoice_candidate' AND a.facts->>'request_id'=r.id::text
     UNION
     SELECT jsonb_array_elements_text(a.facts::jsonb->'candidate_ids') AS inv
     FROM shop_operation_audits a WHERE a.order_id=b.order_id AND a.operation='r4_discover'
         AND a.facts->>'case_id'=c.id::text
 ) candidate
 WHERE candidate.inv ~ '^[A-Za-z0-9_-]{1,100}$'
 AND NOT EXISTS(
     SELECT 1 FROM shop_payment_resolution_observations o
     WHERE o.case_id=c.id AND o.invoice_id=candidate.inv
     AND o.proof::jsonb->'verified'='true'::jsonb
     AND shop_resolution_reference(o.proof->>'reference') IS DISTINCT FROM b.reference
     AND NOT EXISTS(SELECT 1 FROM shop_payment_resolution_observations newer
         WHERE newer.case_id=c.id AND newer.invoice_id=o.invoice_id AND newer.id>o.id)
     AND NOT EXISTS(SELECT 1 FROM shop_payment_resolution_observations ours
         WHERE ours.case_id=c.id AND ours.invoice_id=o.invoice_id
         AND shop_resolution_reference(ours.proof->>'reference')=b.reference)
 )
"""
_CLOSURE_TIME = " OR e->>'verified_at' IS NULL"
_UNBOUND_TIME = """
 OR (b.invoice_id IS NULL AND ((e->>'verified_at')::timestamptz<c.created_at
     OR (e->>'verified_at')::timestamptz>clock_timestamp()
     OR EXISTS(
         SELECT 1 FROM shop_operation_audits a
         JOIN shop_online_payment_requests r ON r.order_id=a.order_id AND r.intent_id=b.id
         WHERE a.facts->>'request_id'=r.id::text
         AND a.operation IN ('online_dispatch_started','online_invoice_dispatched','online_invoice_finished','online_invoice_candidate')
         AND (a.created_at>(e->>'verified_at')::timestamptz OR a.operation IN ('online_dispatch_started','online_invoice_dispatched')
             AND NOT EXISTS(SELECT 1 FROM shop_operation_audits f WHERE f.order_id=a.order_id
                 AND f.operation='online_invoice_finished' AND f.facts->>'request_id'=r.id::text)
             AND NOT COALESCE(
                 e->'abandoned_dispatch'->'worker_stopped'='true'::jsonb
                 AND btrim(e->'abandoned_dispatch'->>'evidence')<>''
                 AND (e->'abandoned_dispatch'->>'stopped_at')::timestamptz
                     BETWEEN a.created_at AND (e->>'verified_at')::timestamptz
                 AND (e->>'verified_at')::timestamptz>=a.created_at+interval '16 minutes', false))
     )))
"""
_CLOSURE_TAIL = " UNION SELECT replacement_invoice_id FROM shop_payment_invoice_corrections WHERE intent_id=b.id) q;"


def _replace(function, old, new):
    bind = op.get_bind()
    definition = bind.execute(sa.text("SELECT pg_get_functiondef(to_regprocedure(:function))"),
                              {"function": function + "()"}).scalar_one()
    if definition is None or definition.count(old) != 1:
        raise RuntimeError(f"Unexpected trigger definition: {function}")
    bind.exec_driver_sql(definition.replace(old, new, 1), execution_options={"no_parameters": True})


def upgrade():
    _replace("protect_shop_resolution_case", " OR p.invoice_id IS NULL", _UNBOUND)
    _replace("validate_shop_bank_closure", _CLOSURE_TAIL, _CANDIDATE + _CLOSURE_TAIL)
    _replace("validate_shop_bank_closure", _CLOSURE_TIME, _UNBOUND_TIME + _CLOSURE_TIME)


def downgrade():
    _replace("validate_shop_bank_closure", _UNBOUND_TIME + _CLOSURE_TIME, _CLOSURE_TIME)
    _replace("validate_shop_bank_closure", _CANDIDATE + _CLOSURE_TAIL, _CLOSURE_TAIL)
    _replace("protect_shop_resolution_case", _UNBOUND, " OR p.invoice_id IS NULL")
