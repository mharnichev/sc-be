# Receipt editing API — contract frozen for FE; implementation/testing in progress

Base cf2b6a33412af79f847bfb1e997a004d2dd2e26f. Isolated checkout:
/tmp/sc-be-receipt-corrections-20261103. No commits/deployment.

All new mutations require existing inventory-post permission (superuser or
can_post_inventory, with the existing master restriction), Idempotency-Key1..128,
and a nonblank reason1..255. Same key+same normalized command returns the CURRENT
receipt without another mutation; changed command with same key returns409.
expected_revision is the document revision from the latest receipt; stale writes
return409. Existing add/post clients stay compatible and now advance revision.

PUT /api/v1/backoffice/inventory/receipts/{receipt_id}/items/{item_id}
JSON: expected_revision, quantity(integer1..1000000), purchase_unit_cost(decimal
0..99999999.99, at most2decimalplaces), allocations(required complete array of
{order_item_id,quantity}; [] explicitly clears planned allocations), reason.
Product/batch/expiry identity stays unchanged; delete+add to replace a draft lot.
Only drafts. Allocations must fit quantity, match the product and ordered active
requirements; the complete replacement is checked against other lines in this
receipt. Plans in other drafts are not physical reservations; posting rechecks.

DELETE same item URI, JSON {expected_revision,reason}: draft only; removes planned
allocations without stock/order/reservation effects. Retries work after deletion.

POST same item URI + /corrections
JSON: expected_revision, quantity(integer0..1000000), purchase_unit_cost, reason.
Posted only. Original line/allocations and receipt posting facts remain immutable.
Zero quantity is an audited reversal, not deletion of original history. Allocation
changes are forbidden; effective quantity must remain >= original allocated qty.
Increase adds unreserved stock. Decrease requires sufficient current FREE stock
and is refused after subsequent product outflows/new reservations because the
system cannot prove lot provenance. Corrections of this same receipt are accounted
for explicitly. Cost-only corrections remain possible after sales: they adjust receipt purchase
valuation and conditionally the current product cost (see below), not historic
sale/order prices, COGS or customer money. There is no FIFO/lot-cost ledger to
retroactively revalue.
Wrong product/lot requires an explicit reversal/new receipt, subject to the same
limits. No automatic unallocation, return, refund, sale or fiscal action.

All mutation responses200 return ReceiptResponse with additive revision,
original_purchase_value and effective_purchase_value; each line keeps original
quantity/purchase_unit_cost and adds effective_quantity,
effective_purchase_unit_cost, effective_purchase_value,last_correction_id.
FE must use effective fields to display corrected posted values; original fields
continue to mean the immutable posted facts. Draft effective fields equal stored
values. GET receipts/list use the same projection.
GET /receipts/{receipt_id}/changes?page=1&page_size=20 returns paginated immutable
command audit (actor, reason, operation, applied revision, before/after, stock and
purchase-value deltas, movement ID). Receipt IDs/item IDs are scoped; wrong IDs404.
Validation422; stale/posted edit/unsafe correction/identity conflict409; forbidden403.

Lock order: receipt, affected order items/orders for draft allocations, product.
Posted corrections preserve allocations/counters and lock only receipt/product.
All balance/movement/audit/version changes commit together. No valuation-only
zero-quantity stock movements are invented. No historical receipt or ledger row
is overwritten. Posting/edits/corrections serialize; replay precedes revision/status
checks. Migration adds revision/audit and PostgreSQL immutable-original/history guards.
The canonical inventory and booking observation migrations both branch from 0144;
0146_merge_inventory_booking combines them without modifying either revision.


## Purchase cost card field (additional authorized requirement)

Product.purchase_unit_cost is nullable decimal money, >=0, two decimal places.
No backfill from sale prices. Present as decimal string or null in private product
responses (stock/search, barcode lookup/bind/create, backoffice product detail).
Zero is a valid cost; null means unknown. Not in Shop/PublicProductResponse,
public search, customer order product projections or public volume variants.
Product.price/shop_price/recommended_retail_price remain unchanged.

Posting updates each product under its existing row lock: ROUND_HALF_UP weighted
average sum(line.quantity * line.purchase_unit_cost)/sum(line.quantity), considering
all that product's lines in THIS receipt. This is not a whole-stock weighted cost.
The internal purchase_cost_receipt_id records the last successfully posted receipt
that set this card field. A retry of posting returns before touching product cost.
Corrections recompute from all effective lines only when THIS receipt is still the
product's cost source; correcting an older receipt does not overwrite a later
receipt's card cost. If all effective quantities of the current source reach zero,
card cost becomes null (no guessed historical fallback). Corrections audit both
receipt valuation and before/after current product cost/source. No retroactive
sale/COGS rewrite. Concurrent posting and corrections serialize on product rows.

## Readiness and verification

This checkout contains a local combined inventory and booking release candidate.
Its intended Alembic head is `0146_merge_inventory_booking`. No deployment is
implied by the presence of this document. Use only an explicitly isolated local
synthetic PostgreSQL database with `test` in its name for integration tests.
On 2026-11-04, the combined inventory and booking regression suite passed 177
tests with no skips; synthetic upgrades from 0144 and each 0145 branch reached
0146 while preserving receipt, ledger and booking history. Empty-feature
0146 → 0144 → 0146 and protected reward-history smoke checks also passed.

## Exact replay classification for FE

Authorization runs before command lookup. An already applied request can therefore
later return403 if access was revoked (or401 if session expired); preserve the
frozen durable request and treat that state as uncertain. Within authorized normal
operation an EXACT same key/URI/normalized payload is checked before CAS, item
existence, posted-status and stock-provenance checks; it returns current receipt200
even after item deletion, later edits/posting, or later stock activity. Thus a409
for a truly exact frozen replay means that command is not recorded as applied.
A changed payload with a reused key is different:409 idempotency conflict can mean
the ORIGINAL command was already applied. Never rebuild expected_revision, reason,
allocations or quantity from a refreshed document when retrying a durable command.


## Migration/working-copy integration

Canonical revisions `0145_receipt_item_changes` and
`0145_booking_funnel_observations` both descend from `0144_master_rewards`.
`0146_merge_inventory_booking` is their additive merge revision. The separate
local-only `0141_receipt_item_changes_local` adapter is not part of this checkout
and must not be used for the canonical release. Downgrade of receipt changes is
refused once audit commands or product cost/source data exist.

For legacy products with BOTH cost and source null, an explicit correction may
establish the source only if this is the demonstrably latest posted receipt for
the product (posted_at descending, nulls last, ID as tie-breaker). Corrections of
older receipts do not backfill/overwrite the card. A manually initialized cost
without a source is preserved. Migration and GET never guess/backfill a cost.
