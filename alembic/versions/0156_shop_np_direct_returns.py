"""Persist isolated direct-prepayment NP physical return facts."""

from alembic import op
import sqlalchemy as sa

revision = "0156_shop_np_direct_returns"
down_revision = "0155_shop_direct_refunds"
branch_labels = None
depends_on = None

_CASES = "shop_np_direct_return_cases"
_OBSERVATIONS = "shop_np_direct_return_observations"
_RECEIPTS = "shop_np_direct_return_receipts"
_TRIGGERS = (
    ("validate_np_direct_case", _CASES),
    ("validate_np_direct_observation", _OBSERVATIONS),
    ("validate_np_direct_receipt", _RECEIPTS),
    ("validate_np_direct_restock", _RECEIPTS),
    ("protect_np_direct_stock", "inventory_movements"),
    ("protect_np_direct_shipment", "order_shipments"),
    ("protect_np_direct_legacy_ttn", "shop_np_return_cases"),
)


def ddl(sql):
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})


def fk(name, table, *, unique=False, uuid=False):
    return sa.Column(name, sa.Uuid() if uuid else sa.Integer(), sa.ForeignKey(f"{table}.id", ondelete="RESTRICT"),
                     nullable=False, unique=unique)


def operator_columns():
    return fk("operator_id", "admin_users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def upgrade():
    op.create_table(
        _CASES, sa.Column("id", sa.Uuid(), primary_key=True), fk("order_id", "orders", unique=True),
        fk("original_shipment_id", "order_shipments", unique=True), fk("fulfillment_id", "shop_fulfillment_facts"),
        fk("sale_id", "shop_mono_checkbox_receipts"),
        sa.Column("original_ttn", sa.String(14), nullable=False), sa.Column("return_ttn", sa.String(14), unique=True,
                                                                          nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(64), nullable=False),
        *operator_columns(),
    )
    op.create_table(
        _OBSERVATIONS, sa.Column("id", sa.Integer(), primary_key=True), fk("case_id", _CASES, uuid=True),
        sa.Column("proof", sa.JSON(), nullable=False), sa.Column("proof_hash", sa.String(64), nullable=False),
        *operator_columns(),
    )
    op.create_index("ix_shop_np_direct_return_observations_case_id", _OBSERVATIONS, ["case_id"])
    op.create_table(
        _RECEIPTS, sa.Column("id", sa.Uuid(), primary_key=True), fk("case_id", _CASES, unique=True, uuid=True),
        fk("order_id", "orders", unique=True), fk("observation_id", _OBSERVATIONS),
        sa.Column("quantities", sa.JSON(), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False), *operator_columns(),
    )
    for table in (_CASES, _OBSERVATIONS, _RECEIPTS):
        op.execute(f"CREATE TRIGGER immutable_np_direct_return BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")

    ddl("""
CREATE FUNCTION validate_np_direct_case() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE state jsonb := NEW.snapshot::jsonb;
BEGIN
 PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
 PERFORM pg_advisory_xact_lock(hashtextextended('np-return-ttn:' || NEW.return_ttn, 0));
 IF NEW.original_ttn !~ '^[0-9]{14}$' OR NEW.return_ttn !~ '^[0-9]{14}$' OR NEW.original_ttn=NEW.return_ttn
    OR EXISTS(SELECT 1 FROM order_shipments WHERE tracking_number=NEW.return_ttn OR provider_reference=NEW.return_ttn)
    OR EXISTS(SELECT 1 FROM shop_np_return_cases WHERE return_ttn=NEW.return_ttn OR order_id=NEW.order_id)
    OR EXISTS(SELECT 1 FROM shop_mono_checkbox_refunds WHERE order_id=NEW.order_id)
    OR state->>'original_ttn' IS DISTINCT FROM NEW.original_ttn
    OR state->>'return_ttn' IS DISTINCT FROM NEW.return_ttn
    OR state->>'order_id' IS DISTINCT FROM NEW.order_id::text
    OR state->>'original_shipment_id' IS DISTINCT FROM NEW.original_shipment_id::text
    OR state->>'fulfillment_id' IS DISTINCT FROM NEW.fulfillment_id::text
    OR state->>'sale_id' IS DISTINCT FROM NEW.sale_id::text
    OR state->>'authority' IS DISTINCT FROM 'test'
    OR state->'profile'->>'environment' IS DISTINCT FROM 'test'
    OR state->'profile'->'approved' IS DISTINCT FROM 'true'::jsonb
    OR state->>'linkage_kind' IS NULL OR state->>'linkage_kind' NOT IN ('easy_return','manual_document')
    OR (CASE WHEN state->>'linkage_kind'='manual_document' THEN
       state->'profile'->'manual_link_allowed' IS DISTINCT FROM 'true'::jsonb
       OR state->'manual_link'->'verified' IS DISTINCT FROM 'true'::jsonb
       OR state->'manual_link'->>'order_id' IS DISTINCT FROM NEW.order_id::text
       OR state->'manual_link'->>'original_shipment_id' IS DISTINCT FROM NEW.original_shipment_id::text
       OR state->'manual_link'->>'original_ttn' IS DISTINCT FROM NEW.original_ttn
       OR state->'manual_link'->>'return_ttn' IS DISTINCT FROM NEW.return_ttn
       OR NOT COALESCE(length(trim(state->'manual_link'->>'source')) BETWEEN 1 AND 100, false)
       OR NOT COALESCE(state->'manual_link'->>'document_id' ~ '^[A-Za-z0-9_.:/-]{1,100}$', false)
       OR NOT COALESCE(state->'manual_link'->>'line_id' ~ '^[A-Za-z0-9_.:/-]{1,100}$', false)
       ELSE state->'manual_link' IS DISTINCT FROM 'null'::jsonb END)
    OR NEW.snapshot_hash IS DISTINCT FROM shop_resolution_hash(state)
    OR NOT EXISTS (
        SELECT 1 FROM orders o
        JOIN shop_mono_checkbox_receipts s ON s.id=NEW.sale_id AND s.order_id=o.id
        JOIN shop_fulfillment_facts f ON f.id=NEW.fulfillment_id AND f.order_id=o.id
        JOIN order_shipments p ON p.id=f.shipment_id AND p.id=NEW.original_shipment_id AND p.order_id=o.id
        WHERE o.id=NEW.order_id AND o.channel='shop' AND NOT o.simulation AND o.fulfillment_method='nova_poshta'
          AND o.status::text IN ('paid','completed') AND s.authority='test' AND s.snapshot_hash=state->>'sale_hash'
          AND f.kind='nova_poshta' AND f.snapshot_hash=state->>'fulfillment_hash'
          AND p.active AND NOT p.simulation AND p.source='attach' AND p.carrier='nova_poshta'
          AND p.return_of_shipment_id IS NULL AND p.handed_over_at=(state->>'dispatched_at')::timestamptz
          AND p.tracking_number=NEW.original_ttn AND p.provider_reference=NEW.original_ttn
          AND NOT EXISTS(SELECT 1 FROM order_shipments extra WHERE extra.order_id=o.id AND extra.id<>p.id)
          AND NOT EXISTS (
            SELECT 1 FROM order_items i FULL JOIN (
                SELECT a.order_item_id, a.quantity FROM shipment_items a WHERE a.shipment_id=p.id
            ) a ON a.order_item_id=i.id
            WHERE (i.order_id=o.id OR a.order_item_id IS NOT NULL)
              AND (i.order_id IS DISTINCT FROM o.id OR i.quantity IS DISTINCT FROM a.quantity)
          )
    ) OR jsonb_typeof(state->'claimed') IS DISTINCT FROM 'object' OR state->'claimed'='{}'::jsonb
    OR EXISTS (
        SELECT 1 FROM jsonb_each(state->'claimed') AS claimed(item_id, quantity)
        WHERE NOT EXISTS (
            SELECT 1 FROM order_items i
            WHERE i.order_id=NEW.order_id AND i.id::text=claimed.item_id
              AND jsonb_typeof(claimed.quantity)='number' AND claimed.quantity::text ~ '^[1-9][0-9]{0,9}$'
              AND (claimed.quantity::text)::bigint BETWEEN 1 AND i.quantity
        )
    ) THEN RAISE EXCEPTION 'Direct NP return requires a canonical dispatched sale and exclusive reverse TTN'; END IF;
 RETURN NEW;
END
$$
""")
    ddl("""
CREATE FUNCTION validate_np_direct_observation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c shop_np_direct_return_cases; p jsonb := NEW.proof::jsonb; first_proof jsonb; prior jsonb;
        original_code text := p->'original'->>'raw_status'; reverse_code text := p->'reverse'->>'raw_status';
        event_time timestamptz := (p->'original'->>'event_at')::timestamptz;
        observed timestamptz := (p->>'observed_at')::timestamptz;
        reverse_time timestamptz := (p->'reverse'->>'event_at')::timestamptz;
        first_code text; first_time timestamptz; k text;
BEGIN
 SELECT * INTO STRICT c FROM shop_np_direct_return_cases WHERE id=NEW.case_id;
 PERFORM 1 FROM orders WHERE id=c.order_id FOR UPDATE;
 SELECT proof::jsonb INTO first_proof FROM shop_np_direct_return_observations WHERE case_id=c.id ORDER BY id LIMIT 1;
 SELECT proof::jsonb INTO prior FROM shop_np_direct_return_observations WHERE case_id=c.id ORDER BY id DESC LIMIT 1;
 first_code := COALESCE(first_proof->'original'->>'raw_status', original_code);
 first_time := COALESCE((first_proof->'original'->>'event_at')::timestamptz, event_time);
 IF EXISTS(SELECT 1 FROM shop_np_direct_return_receipts WHERE case_id=c.id)
    OR NEW.proof_hash IS DISTINCT FROM shop_resolution_hash(p)
    OR p->>'proof_hash' IS DISTINCT FROM shop_resolution_hash(p-'proof_hash')
    OR p->'verified' IS DISTINCT FROM 'true'::jsonb OR p->>'transport' IS DISTINCT FROM 'mock_http'
    OR p->>'authority' IS DISTINCT FROM 'test'
    OR p->>'linkage_authority' IS DISTINCT FROM
       (CASE WHEN c.snapshot->>'linkage_kind'='easy_return' THEN 'test' ELSE 'manual' END)
    OR p->>'linkage_kind' IS DISTINCT FROM c.snapshot->>'linkage_kind'
    OR p->'linkage_verified' IS DISTINCT FROM to_jsonb(c.snapshot->>'linkage_kind'='easy_return')
    OR p->>'profile_id' IS DISTINCT FROM c.snapshot->'profile'->>'id'
    OR p->>'original_ttn' IS DISTINCT FROM c.original_ttn OR p->>'return_ttn' IS DISTINCT FROM c.return_ttn
    OR p->'original'->>'number' IS DISTINCT FROM c.original_ttn OR p->'reverse'->>'number' IS DISTINCT FROM c.return_ttn
    OR observed IS NULL OR observed < NEW.created_at - interval '2 hours' OR observed > NEW.created_at + interval '30 seconds'
    OR first_code IS NULL OR first_code NOT IN ('9','10','11','103') OR original_code IS NULL
    OR NOT (original_code=first_code OR original_code='102' OR
            original_code IN ('9','10','11') AND first_code IN ('9','10','11'))
    OR event_time IS NULL OR event_time < (c.snapshot->>'dispatched_at')::timestamptz - interval '30 seconds'
    OR event_time > observed + interval '30 seconds' OR reverse_time > observed + interval '30 seconds'
    OR p->'reverse'->>'status' IS NULL OR p->'reverse'->>'status' IN ('unknown','requires_review')
    OR (p->'reverse'->>'status'='delivered' AND
        (reverse_time IS NULL OR reverse_time < first_time - interval '30 seconds'))
    OR (prior->'reverse'->>'status'='delivered' AND p->'reverse'->>'status' <> 'delivered')
 THEN RAISE EXCEPTION 'Direct NP tracking requires a dated linked parcel pair and original delivery or refusal'; END IF;
 FOREACH k IN ARRAY ARRAY['original','reverse'] LOOP
   IF prior->k->>'event_at' IS NOT NULL AND (
     p->k->>'event_at' IS NULL OR (p->k->>'event_at')::timestamptz < (prior->k->>'event_at')::timestamptz
     OR ((p->k->>'event_at')::timestamptz = (prior->k->>'event_at')::timestamptz
         AND p->k->>'raw_status' IS DISTINCT FROM prior->k->>'raw_status')
   ) THEN RAISE EXCEPTION 'Direct NP tracking cannot regress'; END IF;
 END LOOP;
 RETURN NEW;
END
$$
""")
    ddl("""
CREATE FUNCTION validate_np_direct_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c shop_np_direct_return_cases; o shop_np_direct_return_observations; state jsonb := NEW.snapshot::jsonb;
        claimed jsonb; lines jsonb := state->'lines'; actual jsonb;
BEGIN
 SELECT * INTO STRICT c FROM shop_np_direct_return_cases WHERE id=NEW.case_id;
 PERFORM 1 FROM orders WHERE id=c.order_id FOR UPDATE;
 SELECT * INTO STRICT o FROM shop_np_direct_return_observations WHERE case_id=c.id ORDER BY id DESC LIMIT 1;
 claimed := c.snapshot::jsonb->'claimed';
 IF NEW.order_id<>c.order_id OR NEW.observation_id<>o.id
    OR o.proof->'reverse'->>'status' IS DISTINCT FROM 'delivered'
    OR o.proof->'reverse'->>'raw_status' NOT IN ('9','10','11')
    OR (o.proof->>'observed_at')::timestamptz < NEW.created_at - interval '2 hours'
    OR (o.proof->>'observed_at')::timestamptz > NEW.created_at + interval '30 seconds'
    OR NEW.proof_hash IS DISTINCT FROM shop_resolution_hash(state)
    OR state->>'receipt_id' IS DISTINCT FROM NEW.id::text OR state->>'order_id' IS DISTINCT FROM NEW.order_id::text
    OR state->>'operator_id' IS DISTINCT FROM NEW.operator_id::text
    OR state->>'case_id' IS DISTINCT FROM c.id::text OR state->>'case_hash' IS DISTINCT FROM c.snapshot_hash
    OR state->>'observation_id' IS DISTINCT FROM o.id::text OR state->>'observation_hash' IS DISTINCT FROM o.proof_hash
    OR state->>'original_ttn' IS DISTINCT FROM c.original_ttn OR state->>'return_ttn' IS DISTINCT FROM c.return_ttn
    OR state->'contents_verified' IS DISTINCT FROM 'true'::jsonb OR state->>'authority' IS DISTINCT FROM 'test'
    OR NOT COALESCE(length(trim(state->>'evidence')) BETWEEN 1 AND 500, false)
    OR jsonb_typeof(lines) IS DISTINCT FROM 'array'
 THEN RAISE EXCEPTION 'Direct NP receipt requires the exact current delivered reverse parcel'; END IF;
 IF jsonb_array_length(lines) <> (SELECT count(*) FROM jsonb_object_keys(claimed))
    OR jsonb_array_length(lines) <> (SELECT count(DISTINCT line->>'order_item_id') FROM jsonb_array_elements(lines) line)
    OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(lines) line
        WHERE NOT COALESCE(
           claimed ? (line->>'order_item_id')
           AND jsonb_typeof(line->'order_item_id')='number'
           AND jsonb_typeof(line->'received_quantity')='number' AND line->>'received_quantity' ~ '^[0-9]{1,10}$'
           AND jsonb_typeof(line->'restock_quantity')='number' AND line->>'restock_quantity' ~ '^[0-9]{1,10}$'
           AND jsonb_typeof(line->'damaged_quantity')='number' AND line->>'damaged_quantity' ~ '^[0-9]{1,10}$'
           AND (line->>'received_quantity')::bigint <= (claimed->>(line->>'order_item_id'))::bigint
           AND (line->>'received_quantity')::bigint =
               (line->>'restock_quantity')::bigint + (line->>'damaged_quantity')::bigint, false)
    ) THEN RAISE EXCEPTION 'Direct NP receipt must inspect every claimed position exactly once'; END IF;
 SELECT COALESCE(jsonb_object_agg(line->>'order_item_id', line->'received_quantity'), '{}'::jsonb)
 INTO actual FROM jsonb_array_elements(lines) line WHERE (line->>'received_quantity')::bigint > 0;
 IF NEW.quantities::jsonb IS DISTINCT FROM actual
    OR EXISTS(SELECT 1 FROM inventory_movements m WHERE m.movement_type::text='customer_return' AND
               (m.order_id=NEW.order_id OR m.order_item_id IN (SELECT id FROM order_items WHERE order_id=NEW.order_id)))
 THEN RAISE EXCEPTION 'Direct NP receipt has inconsistent received quantities or prior returned stock'; END IF;
 RETURN NEW;
END
$$
""")
    ddl("""
CREATE FUNCTION protect_np_direct_stock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE item_order integer;
BEGIN
 IF NEW.movement_type::text <> 'customer_return' THEN
   IF NEW.reference_key LIKE 'shop-np-direct-return:%' THEN
     RAISE EXCEPTION 'Direct NP restock namespace is reserved for inspected returns';
   END IF;
   RETURN NEW;
 END IF;
 SELECT order_id INTO item_order FROM order_items WHERE id=NEW.order_item_id;
 PERFORM 1 FROM orders WHERE id IN (NEW.order_id,item_order) ORDER BY id FOR UPDATE;
 IF (NEW.reference_key LIKE 'shop-np-direct-return:%' OR EXISTS (
     SELECT 1 FROM shop_np_direct_return_cases WHERE order_id IN (NEW.order_id,item_order)
 )) AND NOT EXISTS (
     SELECT 1 FROM shop_np_direct_return_receipts r
     JOIN order_items i ON i.id=NEW.order_item_id AND i.order_id=r.order_id
     CROSS JOIN LATERAL jsonb_array_elements(r.snapshot::jsonb->'lines') line
     WHERE r.order_id=NEW.order_id AND line->>'order_item_id'=i.id::text AND NEW.product_id=i.product_id
       AND NEW.reference_key=concat('shop-np-direct-return:',r.id,':',i.id) AND NEW.admin_user_id=r.operator_id
       AND NEW.quantity::text=line->>'restock_quantity' AND NEW.on_hand_delta=NEW.quantity AND NEW.reserved_delta=0
 ) THEN RAISE EXCEPTION 'Direct NP stock requires the exact inspected restock quantity'; END IF;
 RETURN NEW;
END
$$
""")
    ddl("""
CREATE FUNCTION validate_np_direct_restock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (
   SELECT 1 FROM jsonb_array_elements(NEW.snapshot::jsonb->'lines') line
   JOIN order_items i ON i.id::text=line->>'order_item_id' AND i.order_id=NEW.order_id
   WHERE NOT EXISTS (
     SELECT 1 FROM inventory_movements s WHERE s.order_id=i.order_id AND s.order_item_id=i.id AND s.product_id=i.product_id
       AND s.movement_type::text='shipment' AND s.quantity=i.quantity AND s.on_hand_delta=-i.quantity
       AND s.reserved_delta=-i.quantity AND s.reference_key=concat('order:',i.order_id,':item:',i.id,':shipment')
   ) OR ((line->>'restock_quantity')::bigint > 0 AND NOT EXISTS (
     SELECT 1 FROM inventory_movements m WHERE m.reference_key=concat('shop-np-direct-return:',NEW.id,':',i.id)
       AND m.movement_type::text='customer_return' AND m.order_id=i.order_id AND m.order_item_id=i.id
       AND m.product_id=i.product_id AND m.quantity::text=line->>'restock_quantity'
       AND m.on_hand_delta=m.quantity AND m.reserved_delta=0 AND m.admin_user_id=NEW.operator_id
   ))
 ) THEN RAISE EXCEPTION 'Direct NP inspected stock must post with its receipt against the original shipment ledger'; END IF;
 RETURN NEW;
END
$$
""")
    ddl("""
CREATE FUNCTION protect_np_direct_shipment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identity_change boolean;
BEGIN
 identity_change := TG_OP='INSERT' OR (
   ROW(NEW.order_id,NEW.tracking_number,NEW.provider_reference,NEW.active,NEW.carrier,NEW.source,
       NEW.return_of_shipment_id,NEW.simulation,NEW.handed_over_at)
   IS DISTINCT FROM
   ROW(OLD.order_id,OLD.tracking_number,OLD.provider_reference,OLD.active,OLD.carrier,OLD.source,
       OLD.return_of_shipment_id,OLD.simulation,OLD.handed_over_at)
 );
 IF NOT identity_change THEN RETURN NEW; END IF;
 IF EXISTS(SELECT 1 FROM shop_np_direct_return_cases WHERE order_id=NEW.order_id)
    OR (TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM shop_np_direct_return_cases WHERE original_shipment_id=OLD.id))
 THEN RAISE EXCEPTION 'Direct NP original dispatch identity is immutable'; END IF;
 IF EXISTS(SELECT 1 FROM shop_np_direct_return_cases WHERE return_ttn IN (NEW.tracking_number,NEW.provider_reference))
 THEN RAISE EXCEPTION 'A direct NP reverse TTN cannot be adopted by another shipment'; END IF;
 RETURN NEW;
END
$$
""")
    ddl("""
CREATE FUNCTION protect_np_direct_legacy_ttn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
 PERFORM pg_advisory_xact_lock(hashtextextended('np-return-ttn:' || NEW.return_ttn, 0));
 IF EXISTS(SELECT 1 FROM shop_np_direct_return_cases WHERE return_ttn=NEW.return_ttn OR order_id=NEW.order_id)
 THEN RAISE EXCEPTION 'Direct NP parcel conflicts with an existing NP return'; END IF;
 RETURN NEW;
END
$$
""")
    for name, table in _TRIGGERS:
        if name == "validate_np_direct_restock":
            op.execute(f"CREATE CONSTRAINT TRIGGER {name} AFTER INSERT ON {table} DEFERRABLE INITIALLY DEFERRED "
                       f"FOR EACH ROW EXECUTE FUNCTION {name}()")
        else:
            event = "INSERT OR UPDATE" if name == "protect_np_direct_shipment" else "INSERT"
            # Runs after the R5 shipment identity trigger, which takes the common TTN lock.
            trigger = "z_" + name if name == "protect_np_direct_shipment" else name
            op.execute(f"CREATE TRIGGER {trigger} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}()")


def downgrade():
    if op.get_bind().execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {_CASES})")).scalar_one():
        raise RuntimeError("Direct NP return history exists; downgrade refused")
    for name, table in reversed(_TRIGGERS):
        trigger = "z_" + name if name == "protect_np_direct_shipment" else name
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
        op.execute(f"DROP FUNCTION {name}()")
    for table in (_RECEIPTS, _OBSERVATIONS, _CASES):
        op.drop_table(table)
