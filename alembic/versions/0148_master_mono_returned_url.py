"""Allow exact provider-returned POS payment URLs with independent identity proof."""

from alembic import op
import sqlalchemy as sa


revision = "0148_master_mono_returned_url"
down_revision = "0147_pos_checkout_intents"
branch_labels = None
depends_on = None

_CANONICAL = "OR NEW.page_url IS DISTINCT FROM 'https://pay.mbnk.biz/'||NEW.invoice_id"
_VERIFIED = "OR NOT master_mono_returned_url_valid(NEW.invoice_id, NEW.page_url, NEW.create_proof::jsonb)"


def _replace_binding_guard(old: str, new: str) -> None:
    definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef('validate_master_mono_fact()'::regprocedure)"))
    if not definition or definition.count(old) != 1:
        raise RuntimeError("Unexpected master Mono binding guard")
    op.execute(definition.replace(old, new, 1))


def upgrade():
    op.execute(r"""
        CREATE FUNCTION master_mono_returned_url_valid(invoice_id text, url text, proof jsonb)
        RETURNS boolean LANGUAGE sql STABLE AS $$
        SELECT COALESCE(
            (url = 'https://pay.mbnk.biz/' || invoice_id AND proof->>'page_url' = url)
            OR (
                jsonb_typeof(proof->'observed_at') = 'string'
                AND jsonb_typeof(proof->'created_at') = 'string'
                AND (proof->>'created_at')::timestamptz <= (proof->>'observed_at')::timestamptz
                AND (
                    (url IS NULL AND NOT (proof ? 'page_url') AND (
                        proof->>'status' IN ('processing', 'hold', 'success', 'failure', 'expired', 'reversed')
                        OR (proof->>'status' = 'created' AND proof->'has_cancellations' = 'true'::jsonb)
                    ))
                    OR (
                        proof->'returned_page_verified' = 'true'::jsonb
                        AND proof->>'page_url' = url
                        AND length(url) BETWEEN 22 AND 240
                        AND url LIKE 'https://pay.mbnk.biz/%'
                        AND url COLLATE "C" !~ '[^!-~]|#'
                        AND position(chr(92) in url) = 0
                        AND url !~* '%(0[0-9a-f]|1[0-9a-f]|7f|5c)'
                        AND position('%' in regexp_replace(url, '%[0-9a-fA-F]{2}', '', 'g')) = 0
                        AND route COLLATE "C" !~ '["<>`{}^|]'
                        AND query !~ $rx$["'<>]$rx$
                        AND replace(lower(route), '%2e', '.') !~ '(^|/)\.{1,2}(/|$)'
                        AND (route = invoice_id OR route COLLATE "C" !~ '^[A-Za-z0-9_-]{1,100}$')
                    )
                )
            ), false
        )
        FROM (
            SELECT split_part(substring(url from 22), '?', 1) AS route,
                   CASE WHEN position('?' in url) > 0
                        THEN substring(url from position('?' in url) + 1) ELSE '' END AS query
        ) AS url_parts
        $$
    """)
    _replace_binding_guard(_CANONICAL, _VERIFIED)


def downgrade():
    op.execute("LOCK TABLE master_mono_bindings IN SHARE ROW EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM master_mono_bindings
            WHERE create_request_id IS NOT NULL AND page_url IS DISTINCT FROM 'https://pay.mbnk.biz/' || invoice_id
        )
    """)):
        raise RuntimeError("Preserve verified POS bindings; populated ledger cannot downgrade")
    _replace_binding_guard(_VERIFIED, _CANONICAL)
    op.execute("DROP FUNCTION master_mono_returned_url_valid(text, text, jsonb)")
