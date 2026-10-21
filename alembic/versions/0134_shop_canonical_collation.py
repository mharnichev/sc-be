"""Bytewise canonical keys, with immutable-history compatibility in both directions."""
from alembic import op
import sqlalchemy as sa

revision = "0134_shop_canonical_collation"
down_revision = "0133_master_mono_live"
branch_labels = None
depends_on = None

# Keep the 0126 serialization contract; only object-key collation changes.
LEGACY_BODY = """
      SELECT CASE jsonb_typeof(j)
      WHEN 'object' THEN '{'||COALESCE((SELECT string_agg(to_jsonb(key)::text||':'||shop_payment_canonical(value),',' ORDER BY key) FROM jsonb_each(j)),'')||'}'
      WHEN 'array' THEN '['||COALESCE((SELECT string_agg(shop_payment_canonical(value),',' ORDER BY ord) FROM jsonb_array_elements(j) WITH ORDINALITY a(value,ord)),'')||']'
      ELSE j::text END
"""
BYTEWISE_BODY = LEGACY_BODY.replace("ORDER BY key)", 'ORDER BY key COLLATE "C")')

# Explicit persisted inputs to the shared canonicalizer and its R4 dependencies.
# Each tuple is (JSON expression, stored digest, optional canonical text).
CONTRACTS = {
    "shop_checkbox_registers": (("profile", "profile_hash", None),),
    "shop_payment_confirmations": (("snapshot", "snapshot_hash", None),),
    "shop_payment_intents": (("profile", "profile_hash", None),),
    "shop_payment_evidence": (("proof", "proof_hash", "proof_canonical"),),
    "shop_payment_resolution_cases": (("profile", "profile_hash", None), ("snapshot", "snapshot_hash", None)),
    "shop_payment_resolution_observations": (("proof", "proof_hash", "proof_canonical"),),
    "shop_payment_invoice_corrections": (("snapshot", "snapshot_hash", None),),
    "shop_payment_bank_closures": (("policy", "policy_hash", None), ("snapshot", "snapshot_hash", None)),
    "shop_payment_resolution_finalizations": (("snapshot", "snapshot_hash", None),),
    "shop_np_return_cases": (("profile", "profile_hash", None), ("snapshot", "snapshot_hash", None)),
    "shop_np_return_observations": (("proof", "proof_hash", "proof_canonical"),),
    "shop_np_return_receipts": (("snapshot", "snapshot_hash", None),),
    "shop_np_unpaid_closures": (("snapshot", "snapshot_hash", None),),
    "shop_mono_checkbox_receipts": (("snapshot", "snapshot_hash", None), ("proof", "proof_hash", "proof_canonical")),
    "shop_mono_checkbox_observations": (("proof", "proof_hash", None),),
    "shop_mono_checkbox_artifacts": (("proof", "proof_hash", None),),
    "master_mono_create_requests": (("profile", "profile_hash", None), ("basket", "basket_hash", None)),
    "master_mono_bindings": (("profile", "profile_hash", None),),
    "master_mono_payments": (("proof", "proof_hash", None),),
    "master_mono_receipts": (("proof", "proof_hash", None),),
    "master_mono_observations": (("proof", "proof_hash", None),),
    "master_mono_artifacts": (("proof", "proof_hash", None),),
}

# Self-digests are distinct from the outer stored digest. The observation
# wrappers contain independently signed normalized payment/fiscal proofs.
SELF_PROOFS = {
    "shop_payment_evidence": ("proof::jsonb",),
    "shop_payment_resolution_observations": ("proof::jsonb",),
    "shop_np_return_observations": ("proof::jsonb",),
    "shop_mono_checkbox_receipts": ("proof::jsonb",),
    "shop_mono_checkbox_artifacts": ("proof::jsonb",),
    "shop_mono_checkbox_observations": ("proof::jsonb->'payment'", "proof::jsonb->'fiscal'"),
    "master_mono_bindings": ("create_proof::jsonb",),
    "master_mono_payments": ("proof::jsonb",),
    "master_mono_receipts": ("proof::jsonb",),
    "master_mono_artifacts": ("proof::jsonb",),
    "master_mono_observations": ("proof::jsonb->'payment'", "proof::jsonb->'fiscal'"),
}


def canonical(value):
    return f"shop_payment_canonical(COALESCE(({value})::jsonb,'null'::jsonb))"


def digest(value):
    return f"encode(sha256(convert_to({canonical(value)},'UTF8')),'hex')"


def audit_history(connection):
    for table, contracts in CONTRACTS.items():
        checks = []
        for value, stored_hash, stored_text in contracts:
            # Both SQL NULLs are an explicitly absent optional observation.
            checks.append(f"(NOT ({value} IS NULL AND {stored_hash} IS NULL) AND "
                          f"{stored_hash} IS DISTINCT FROM {digest(value)})")
            if stored_text:
                checks.append(f"{stored_text} IS DISTINCT FROM {canonical(value)}")
        for value in SELF_PROOFS.get(table, ()):
            # Manual COD evidence has no provider self-digest. Only validate a
            # self-digest when present; business/provenance guards are unchanged.
            unsigned = f"({value}) - 'proof_hash'"
            checks.append(
                f"(jsonb_typeof(({value}))='object' AND ({value}) ? 'proof_hash' AND "
                f"({value})->>'proof_hash' IS DISTINCT FROM {digest(unsigned)})"
            )
        if connection.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE {' OR '.join(checks)})")):
            raise RuntimeError(f"Canonical collation history incompatible: {table}; no facts or hashes changed")


def switch(expected, target):
    context = op.get_context()
    connection = op.get_bind()
    if not context.impl.transactional_ddl or not connection.in_transaction():
        raise RuntimeError("Canonical collation migration requires transactional DDL")
    if connection.scalar(sa.text("SHOW server_encoding")) != "UTF8":
        raise RuntimeError("Canonical collation migration requires UTF8")
    if connection.scalar(sa.text("SHOW transaction_isolation")) != "read committed":
        raise RuntimeError("Canonical collation migration requires read committed isolation")
    function = connection.execute(sa.text("""SELECT p.prosrc, p.provolatile='i' AND p.proisstrict
        AND NOT p.prosecdef AND p.proconfig IS NULL AND l.lanname='sql'
        AND p.prorettype='text'::regtype AS expected_flags
        FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
        WHERE p.oid='shop_payment_canonical(jsonb)'::regprocedure""")).mappings().one()
    if not function["expected_flags"] or function["prosrc"].split() != expected.split():
        raise RuntimeError("Unexpected canonicalizer definition; migration refused")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("LOCK TABLE " + ", ".join(sorted(CONTRACTS)) + " IN SHARE ROW EXCLUSIVE MODE")
    # Replacing first lets every recursive call use the target ordering. Any
    # audit failure rolls back this DDL and Alembic version with the transaction.
    op.execute("CREATE OR REPLACE FUNCTION shop_payment_canonical(j jsonb) RETURNS text "
               "LANGUAGE SQL IMMUTABLE STRICT AS $$" + target + "$$")
    audit_history(connection)


def upgrade():
    switch(LEGACY_BODY, BYTEWISE_BODY)


def downgrade():
    switch(BYTEWISE_BODY, LEGACY_BODY)
