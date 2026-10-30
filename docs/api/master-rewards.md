# Internal master rewards API v1 (manual only)

Base: `/api/v1/backoffice/master-rewards`. All responses private/no-store. No
provider calls, notifications, automatic accrual, historical backfill or money
movement. Base commit: 6a9742726c9e0e505cf2d5775cae2d736daa0eb7.

## Authorization and privacy
Management requires a currently active `is_superuser`. Master reads require an
active account and current `Master.admin_user_id` link; QR/sales grants are NOT
required. Rows additionally pin beneficiary_user_id at accrual: a different
account later linked to the same master must never inherit previous account rows.
GET /me endpoints cannot accept a client master_id. No customer/contact/provider
identifiers in responses. No reward amounts/rates/reasons in external telemetry.

## Money and identity
All money: integer UAH kopecks, currency="UAH". `rate_bps`: integer 0..10000
(100 bps = 1%), initially null. Net base is existing immutable order-item total
AFTER discounts, not current catalog price; no shipping/provider fee.
Additional order-level discount is allocated proportionally in integer kopecks
using the existing fiscal net_lines rule; remainder ties sort by order_item_id.
Allocated line bases sum exactly to order total.
Reward = ROUND_HALF_UP(base_minor * rate_bps / 10000), once for the whole line.
Every entry covers the whole existing order item, unique across all masters.
Admin explicitly selects master; order creator is not overwritten.
Sale must already be completed + verified paid + handed over, non-simulation and
non-test authority, from POS mono or canonical Shop completion. No financial
operation is executed to qualify it. Known returns/reversals block admission.
Rate snapshot is taken at manual accrual, not sale creation. Past sales require
an explicit individual admin command; there is no automatic historical job.

## Wire contract
JSON rejects unknown fields. Commands require UUID `Idempotency-Key`. Same key,
operator, command and payload replays original response after current permission
check; conflicting payload/operation = 409. Error body: `{detail:{code: string}}`
(no submitted values). Lists: `{items: [...], total, page, page_size}`; page starts
at 1, page_size 1..100. Dates YYYY-MM-DD, inclusive Europe/Kyiv sale dates for
entries/summary; settlement filters refer independently to actual `paid_at`.
All timestamps ISO8601 with timezone.

- GET `/policy` admin: `{revision, rate_bps: int|null, currency:"UAH"}`.
- PUT `/policy` admin: `{expected_revision:int>=0, rate_bps:int|null, reason:string}`.
  Null unsets rate for future entries; existing entries do not change.
- GET `/masters` admin: `{items:[{id, name}]}` active masters with active linked
  accounts (no contacts). Explicit picker; not public Master DTO.
- GET `/candidates?order_id=int` admin: eligible existing items only,
  `{items:[{order_id, order_item_id, order_revision, product_id, product_name,
  quantity, base_minor, sold_at}], currency:"UAH"}`. No customer data.
- POST `/entries` admin: `{order_id, order_item_id, master_id,
  expected_order_revision, expected_policy_revision, reason}`. 201 Entry.
- GET `/entries` admin; GET `/me/entries` master. Optional `date_from`, `date_to`,
  `page`, `page_size`; admin only optional master_id. Entry:
  `{id, revision, order_id, order_item_id, master_id, master_name, product_id,
  product_name, quantity, base_minor, rate_bps, reward_minor, currency, sold_at,
  created_at, needs_review, settlement_id:int|null, status:pending|needs_review|transferred}`.
  Transferred plus needs_review remains transferred; never automatic debt.
- GET `/summary` admin; GET `/me/summary` master. Optional sale dates; admin only
  master_id. `{currency:"UAH",items:[{master_id, master_name,sales_count,units,
  sales_net_minor,accrued_minor,transferred_minor,pending_minor,held_minor,balance_minor}]}`.
  balance=accrued-transferred; includes held balance, not a debt/withdrawal.
- POST `/entries/{id}/review` admin:
  `{expected_revision, needs_review:bool, reason}` -> Entry. Marks/unmarks only
  the manual issue flag. No refund, reversal, debt or adjustment of money.
  Audit reason mandatory, including when clearing a flag.
- POST `/settlements/preview` admin:
  `{master_id, entries:[{id, expected_revision}], expected_total_minor:int>=0}`
  -> `{master_id,entry_ids,total_minor,currency:"UAH"}`. 1..100 distinct entries;
  whole entries only, same pinned master AND beneficiary account, unclosed,
  no review/known-return block. Submitted total is compared, never trusted.
- POST `/settlements` admin: same fields plus
  `{paid_at:timezone-aware datetime, reason:string, manual_payment_confirmed:true}`
  -> 201 Settlement. Records a single already-performed off-system manual
  handover, immediately immutable/closed. Positive total only, not future date.
  No draft state and no `/close` command required in v1.
- GET `/settlements` admin; GET `/me/settlements` master. Admin optional master_id;
  optional paid date_from/date_to, paging. Settlement:
  `{id,master_id,master_name,total_minor,currency,paid_at,created_at,
  entry_ids,status:"transferred"}`. Master history excludes admin free-text reasons.
- GET `/operations/{key}` admin: durable recovery of own command response,
  `{operation, response}`; unknown key = 404. Never exposes another operator key.

Conflicts: `revision_conflict`, `policy_not_configured`, `entry_exists`,
`sale_not_eligible`, `entry_not_available`, `total_mismatch`, `idempotency_conflict`.
Missing/foreign resource =404, unauthorized=401/403, invalid command=422.

## Accounting/audit/returns
Only admin POST /entries creates entries; no sale/stock/provider hook. Policy,
entries, review decisions and settlement each append a private audit with actor,
time, reason, source IDs and computed totals. Immutable financial snapshots and
unique allocation prevent duplicate accrual and double closure across sessions.
Known financial/physical return facts make unclosed entries unavailable for
settlement until resolved externally; admin review flag also blocks closure.
After transfer, an issue is marked needs_review only: original total and closure
remain unchanged. No debt, automatic withholding or refund implementation.
New roles/private rate fields must not be added to shared/public DTOs.

## FE agreement
Admin `/admin/sales-rewards`; master `/my-sales-rewards`. Policy starts unset,
no invented default. Store command key across uncertain responses, retry same
payload/key or GET /operations. Preview is not reservation: final command rechecks
locks/revisions. Sales period and payment date are separate filters. Disable
telemetry/replay for these pages. All calculations and access decisions are BE-owned.
