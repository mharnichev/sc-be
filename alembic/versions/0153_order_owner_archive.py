"""Permit audited order cancellation without rewriting payment or fulfillment history."""

from alembic import op
import sqlalchemy as sa


revision = "0153_order_owner_archive"
down_revision = "0152_shop_online_admin_cancel"
branch_labels = None
depends_on = None


_AUTHORIZED = "order_owner_archive_authorized(OLD.id, OLD.revision)"
_STATUS_EXCEPTION = f" AND NOT (NEW.status::text='cancelled' AND {_AUTHORIZED})"
_STOCK_EXCEPTION = "\n      IF order_owner_archive_release(NEW) THEN RETURN NEW; END IF;"
_EDITS = {
    "protect_shop_order": (
        "AND NOT shop_resolution_can_close(OLD.id)",
        f"AND NOT shop_resolution_can_close(OLD.id) AND NOT {_AUTHORIZED}",
    ),
    "protect_shop_r2_order": (
        "AND NOT shop_online_cancel_authorized(OLD.id, OLD.revision)",
        f"AND NOT shop_online_cancel_authorized(OLD.id, OLD.revision) AND NOT {_AUTHORIZED}",
    ),
    "protect_shop_cancelled_invoice_binding": (
        "WHERE order_id=OLD.order_id AND operation='online_cancel_undispatched'",
        "WHERE order_id=OLD.order_id AND (operation='online_cancel_undispatched'"
        " OR operation='owner_cancel_order' AND shop_online_dispatch_pending(OLD.order_id))",
    ),
    "guard_qr_sale_mutation": (
        "AND q.payment_status = 'paid' AND q.receipt_status = 'issued'))",
        "AND q.payment_status = 'paid' AND q.receipt_status = 'issued')" + _STATUS_EXCEPTION + ")",
    ),
    "guard_master_mono_owner": (
        "OR NEW.status IS DISTINCT FROM OLD.status AND NEW.status::text!='completed'",
        "OR NEW.status IS DISTINCT FROM OLD.status AND NEW.status::text!='completed'" + _STATUS_EXCEPTION,
    ),
    "protect_shop_resolution_stock": (
        "IF c.id IS NULL THEN RETURN NEW;END IF;",
        "IF c.id IS NULL THEN RETURN NEW;END IF;" + _STOCK_EXCEPTION,
    ),
    "protect_master_dispatch_stock": (
        "IF q.id IS NULL THEN RETURN NEW; END IF;",
        "IF q.id IS NULL THEN RETURN NEW; END IF;" + _STOCK_EXCEPTION,
    ),
    "require_master_live_handover": (
        "IF q.id IS NULL THEN RETURN NEW; END IF;",
        "IF q.id IS NULL THEN RETURN NEW; END IF;\n"
        "      IF TG_TABLE_NAME='inventory_movements' THEN" + _STOCK_EXCEPTION + "\n      END IF;",
    ),
}


def replace_guards(*, reverse=False):
    for name, (before, after) in _EDITS.items():
        source = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"),
            {"name": f"{name}()"})
        old, new = (after, before) if reverse else (before, after)
        if not source or source.count(old) != 1:
            raise RuntimeError(f"Unexpected owner archive ledger guard: {name}")
        op.execute(source.replace(old, new, 1))


def upgrade():
    op.execute("""
    CREATE UNIQUE INDEX uq_order_owner_archive_audit ON shop_operation_audits (order_id)
      WHERE operation='owner_cancel_order';
    CREATE INDEX ix_order_owner_archive_audits ON shop_operation_audits (order_id,id)
      WHERE operation IN ('owner_cancel_order','owner_cancel_late_payment');

    CREATE FUNCTION order_owner_archive_authorized(oid integer, expected_revision integer) RETURNS boolean
    LANGUAGE sql STABLE AS $$
      SELECT EXISTS (
        SELECT 1 FROM shop_operation_audits a
        WHERE a.order_id=oid AND a.operation='owner_cancel_order'
          AND a.facts::jsonb->'expected_revision'=to_jsonb(expected_revision)
      )
    $$;

    CREATE FUNCTION order_owner_archive_release(m inventory_movements) RETURNS boolean
    LANGUAGE sql STABLE AS $$
      SELECT COALESCE(m.movement_type::text='reservation_release' AND m.quantity>0
        AND m.on_hand_delta=0 AND m.reserved_delta=-m.quantity, false)
        AND EXISTS (
          SELECT 1 FROM shop_operation_audits a JOIN orders o ON o.id=a.order_id
          WHERE a.order_id=m.order_id AND a.operation='owner_cancel_order' AND a.operator_id=m.admin_user_id
            AND (o.status::text!='cancelled' AND a.facts::jsonb->'expected_revision'=to_jsonb(o.revision)
              OR o.status::text='cancelled' AND a.facts::jsonb->'expected_revision'=to_jsonb(o.revision-1))
            AND (a.facts::jsonb->'reserve_release') @> jsonb_build_array(jsonb_build_object(
              'order_item_id',m.order_item_id,'product_id',m.product_id,'quantity',m.quantity,'reference_key',m.reference_key))
        )
    $$;

    CREATE FUNCTION validate_order_owner_archive_audit() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE o orders%ROWTYPE; snapshot jsonb; plan jsonb; expected jsonb; planned jsonb; over_reserved boolean;
    BEGIN
      IF NEW.operation!='owner_cancel_order' THEN RETURN NEW; END IF;
      SELECT * INTO o FROM orders WHERE id=NEW.order_id FOR UPDATE;
      snapshot:=NEW.facts::jsonb;
      plan:=snapshot->'reserve_release';
      IF o.id IS NULL OR o.status::text='cancelled'
        OR snapshot->'expected_revision' IS DISTINCT FROM to_jsonb(o.revision)
        OR jsonb_typeof(plan) IS DISTINCT FROM 'array'
        OR jsonb_typeof(snapshot->'inventory_reconciliation') IS DISTINCT FROM 'boolean'
      THEN RAISE EXCEPTION 'Owner archive requires a current order and an exact reserve plan'; END IF;
      PERFORM 1 FROM admin_users a WHERE a.id=NEW.operator_id AND a.is_active
        AND (a.is_superuser OR (a.can_record_sales AND a.can_post_inventory
          AND (a.can_manage_shipments OR NOT (o.fulfillment_method IS NOT DISTINCT FROM 'nova_poshta'
            OR (o.fulfillment_method IS NULL AND COALESCE(o.shipping_company,'') IN ('nova_poshta','novaPoshta','novaPost'))))
          AND NOT EXISTS(SELECT 1 FROM masters m WHERE m.admin_user_id=a.id)))
        FOR SHARE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Owner archive requires order and inventory permissions'; END IF;

      SELECT COALESCE(jsonb_agg(jsonb_build_object('order_item_id',i.id,'product_id',i.product_id,
        'quantity',net.quantity,'reference_key','order:'||o.id||':item:'||i.id||chr(58)||'owner-archive-release')
        ORDER BY i.id) FILTER (WHERE net.quantity<=i.quantity), '[]'::jsonb),
        COALESCE(bool_or(net.quantity>i.quantity),false) INTO expected,over_reserved
      FROM order_items i
      JOIN LATERAL (
        SELECT sum(m.reserved_delta) AS quantity FROM inventory_movements m
        WHERE m.order_id=o.id AND m.order_item_id=i.id AND m.product_id=i.product_id
      ) net ON net.quantity>0
      WHERE i.order_id=o.id;
      SELECT COALESCE(jsonb_agg(line ORDER BY position), '[]'::jsonb) INTO planned
      FROM jsonb_array_elements(expected) WITH ORDINALITY AS entries(line,position)
      WHERE plan @> jsonb_build_array(line);
      IF plan IS DISTINCT FROM planned OR (snapshot->'inventory_reconciliation'='false'::jsonb
        AND (plan IS DISTINCT FROM expected OR over_reserved))
      THEN RAISE EXCEPTION 'Owner archive can release only its exact remaining owned reserve'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER validate_order_owner_archive_audit BEFORE INSERT ON shop_operation_audits
      FOR EACH ROW WHEN (NEW.operation='owner_cancel_order') EXECUTE FUNCTION validate_order_owner_archive_audit();

    CREATE FUNCTION guard_order_owner_archive_status() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.status::text='cancelled' AND NEW.status IS DISTINCT FROM OLD.status
        AND EXISTS (SELECT 1 FROM shop_operation_audits a WHERE a.order_id=OLD.id AND a.operation='owner_cancel_order')
      THEN RAISE EXCEPTION 'An owner-archived order cannot reopen'; END IF;
      IF NEW.status::text='cancelled' AND OLD.status::text!='cancelled'
        AND order_owner_archive_authorized(OLD.id,OLD.revision)
        AND (NEW.revision IS DISTINCT FROM OLD.revision+1
          OR (to_jsonb(NEW)-ARRAY['status','revision','updated_at']) IS DISTINCT FROM
            (to_jsonb(OLD)-ARRAY['status','revision','updated_at']))
      THEN RAISE EXCEPTION 'Owner archive may change only lifecycle status and revision'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER guard_order_owner_archive_status BEFORE UPDATE ON orders
      FOR EACH ROW EXECUTE FUNCTION guard_order_owner_archive_status();

    CREATE FUNCTION guard_order_owner_archive_movement() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='INSERT' THEN
        IF NEW.reference_key LIKE ('%'||chr(58)||'owner-archive-release') AND NOT order_owner_archive_release(NEW)
        THEN RAISE EXCEPTION 'Owner archive stock release requires exact authorization'; END IF;
        RETURN NEW;
      END IF;
      IF OLD.reference_key LIKE ('%'||chr(58)||'owner-archive-release')
        OR TG_OP='UPDATE' AND NEW.reference_key LIKE ('%'||chr(58)||'owner-archive-release')
      THEN RAISE EXCEPTION 'Owner archive stock release is immutable'; END IF;
      RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
    END $$;
    CREATE TRIGGER guard_order_owner_archive_movement BEFORE INSERT OR UPDATE OR DELETE ON inventory_movements
      FOR EACH ROW EXECUTE FUNCTION guard_order_owner_archive_movement();

    CREATE FUNCTION require_order_owner_archive_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM orders o WHERE o.id=NEW.order_id AND o.status::text='cancelled'
        AND NEW.facts::jsonb->'expected_revision'=to_jsonb(o.revision-1))
        OR EXISTS (
          SELECT 1 FROM jsonb_array_elements(NEW.facts::jsonb->'reserve_release') AS p(line)
          WHERE NOT EXISTS (SELECT 1 FROM inventory_movements m WHERE m.reference_key=p.line->>'reference_key'
            AND order_owner_archive_release(m))
        )
      THEN RAISE EXCEPTION 'Owner archive audit requires atomic cancellation and owned stock release'; END IF;
      RETURN NEW;
    END $$;
    CREATE CONSTRAINT TRIGGER require_order_owner_archive_complete AFTER INSERT ON shop_operation_audits
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.operation='owner_cancel_order')
      EXECUTE FUNCTION require_order_owner_archive_complete();
    """)
    replace_guards()


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM shop_operation_audits WHERE operation='owner_cancel_order')")):
        raise RuntimeError("Audited owner order archives must be preserved")
    replace_guards(reverse=True)
    for name, table in (
        ("require_order_owner_archive_complete", "shop_operation_audits"),
        ("guard_order_owner_archive_movement", "inventory_movements"),
        ("guard_order_owner_archive_status", "orders"),
        ("validate_order_owner_archive_audit", "shop_operation_audits"),
    ):
        op.execute(f"DROP TRIGGER {name} ON {table}")
        op.execute(f"DROP FUNCTION {name}()")
    op.execute("DROP FUNCTION order_owner_archive_release(inventory_movements)")
    op.execute("DROP FUNCTION order_owner_archive_authorized(integer,integer)")
    op.execute("DROP INDEX ix_order_owner_archive_audits")
    op.execute("DROP INDEX uq_order_owner_archive_audit")
