"""Authorize Shop cancellation only after recorded Mono invoice deactivation."""

from alembic import op
import sqlalchemy as sa

revision = "0160_shop_mono_unpaid"
down_revision = "0159_shop_balance"
branch_labels = None
depends_on = None

ELIGIBLE = "shop_online_cancel_eligible(oid) AND EXISTS ("
AUTHORIZATION = """shop_mono_unpaid_eligible(oid, false) AND EXISTS (
        SELECT 1 FROM shop_operation_audits a
        JOIN shop_payment_intents i ON i.order_id=a.order_id
        JOIN shop_online_payment_requests r ON r.order_id=a.order_id
        WHERE a.order_id=oid AND a.operation='online_unpaid_removed'
          AND a.facts->>'intent_id'=i.id
          AND a.facts->>'request_id'=r.id::text
          AND a.facts->>'invoice_id'=COALESCE(i.invoice_id, (
            SELECT c.facts->>'invoice_id' FROM shop_operation_audits c
            WHERE c.order_id=oid AND c.operation='online_invoice_candidate'
              AND c.facts->>'request_id'=r.id::text LIMIT 1
          ))
          AND a.facts->>'reference'=i.reference
          AND a.facts->>'amount_minor'=i.amount_minor::text
          AND a.facts->>'removal_http_status'='200'
          AND EXISTS (
            SELECT 1 FROM shop_operation_audits f
            WHERE f.order_id=oid AND f.operation='online_unpaid_finalize'
              AND f.facts::jsonb=jsonb_build_object(
                'removal_audit_id',a.id,'expected_revision',expected_revision
              )
          )
      )"""


def rewrite(signature, old, new):
    sql = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"), {"name": signature})
    if not sql or sql.count(old) != 1:
        raise RuntimeError(f"Unexpected Shop cancellation guard: {signature}")
    op.execute(sql.replace(old, new, 1))


def upgrade():
    connection = op.get_bind()
    source = connection.scalar(sa.text("SELECT pg_get_functiondef('shop_online_cancel_eligible(integer)'::regprocedure)"))
    start = source.index("          AND (\n")
    end = source.index("\n      )", start)
    source = source[:start] + """          AND EXISTS (
            SELECT 1 FROM shop_online_payment_requests r
            JOIN shop_payment_intents i ON i.id=r.intent_id AND i.order_id=r.order_id
            WHERE r.order_id=oid AND i.mode='prepayment'
              AND EXISTS (
                SELECT 1 FROM shop_operation_audits f
                WHERE f.order_id=oid AND f.operation='online_invoice_finished'
                  AND f.facts->>'request_id'=r.id::text
              )
          )""" + source[end:]
    source = source.replace("shop_online_cancel_eligible(oid integer)", "shop_mono_unpaid_eligible(oid integer, bank_only boolean)")
    source = source.replace("o.status::text IN ('pending','confirmed')",
        "(NOT bank_only AND o.status::text IN ('pending','confirmed') OR bank_only AND o.status::text='cancelled')")
    op.execute(source)
    rewrite("shop_online_cancel_authorized(integer,integer)", ELIGIBLE, "(" + AUTHORIZATION + ") OR " + ELIGIBLE)


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM shop_operation_audits WHERE operation IN ('online_unpaid_dispatched','online_unpaid_removed'))"
    )):
        raise RuntimeError("Cannot remove guards with recorded Mono invoice deactivation attempts")
    rewrite("shop_online_cancel_authorized(integer,integer)", "(" + AUTHORIZATION + ") OR " + ELIGIBLE, ELIGIBLE)
    op.execute("DROP FUNCTION shop_mono_unpaid_eligible(integer, boolean)")
