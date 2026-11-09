"""Treat the authenticated Mono payment URL path as opaque."""

from alembic import op
import sqlalchemy as sa


revision = "0149_master_mono_opaque_route"
down_revision = "0148_master_mono_returned_url"
branch_labels = None
depends_on = None

_DOT_GUARD = r"AND replace(lower(route), '%2e', '.') !~ '(^|/)\.{1,2}(/|$)'"
_SLUG_GUARD = "\n                        AND (route = invoice_id OR route COLLATE \"C\" !~ '^[A-Za-z0-9_-]{1,100}$')"


def _replace_guard(old: str, new: str) -> None:
    definition = op.get_bind().scalar(sa.text(
        "SELECT pg_get_functiondef('master_mono_returned_url_valid(text,text,jsonb)'::regprocedure)"
    ))
    if not definition or definition.count(old) != 1:
        raise RuntimeError("Unexpected master Mono returned URL guard")
    op.execute(definition.replace(old, new, 1))


def upgrade():
    _replace_guard(_DOT_GUARD + _SLUG_GUARD, _DOT_GUARD)


def downgrade():
    op.execute("LOCK TABLE master_mono_bindings IN SHARE ROW EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM master_mono_bindings
            WHERE create_request_id IS NOT NULL
              AND split_part(substring(page_url from 22), '?', 1) COLLATE "C" ~ '^[A-Za-z0-9_-]{1,100}$'
              AND split_part(substring(page_url from 22), '?', 1) <> invoice_id
        )
    """)):
        raise RuntimeError("Preserve verified POS bindings; short POS payment routes exist")
    _replace_guard(_DOT_GUARD, _DOT_GUARD + _SLUG_GUARD)
