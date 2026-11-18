"""Authorize Shop order cancellation from an exact unpaid expiry observation."""


from alembic import op

revision = "0161_shop_mono_expired"
down_revision = "0160_shop_mono_unpaid"
branch_labels = None
depends_on = None

OLD = "a.operation='online_unpaid_removed'"
NEW = "a.operation IN ('online_unpaid_removed','online_unpaid_expired')"
OLD_PROOF = "a.facts->>'removal_http_status'='200'"
NEW_PROOF = """(
            a.operation='online_unpaid_removed' AND a.facts->>'removal_http_status'='200'
            OR a.operation='online_unpaid_expired' AND a.facts->>'status'='expired'
               AND a.facts->>'currency_code'='980' AND a.facts->>'final_amount_minor'='0'
               AND a.facts->>'has_cancellations'='false'
          )"""


def rewrite(before, after, proof_before, proof_after):
    definition = op.get_bind().exec_driver_sql(
        "SELECT pg_get_functiondef('shop_online_cancel_authorized(integer,integer)'::regprocedure)"
    ).scalar_one()
    if definition.count(before) != 1 or definition.count(proof_before) != 1:
        raise RuntimeError("Unexpected Shop cancellation authorization")
    op.execute(definition.replace(before, after).replace(proof_before, proof_after))


def upgrade():
    rewrite(OLD, NEW, OLD_PROOF, NEW_PROOF)


def downgrade():
    rewrite(NEW, OLD, NEW_PROOF, OLD_PROOF)
