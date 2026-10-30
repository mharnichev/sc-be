# Manual master rewards — local implementation evidence

Base: 6a9742726c9e0e505cf2d5775cae2d736daa0eb7. Isolated detached clone:
`/tmp/sc-be-master-rewards-20261030`. Original dirty checkout is not modified.
API: [master-rewards.md](../api/master-rewards.md).

Only explicit admin commands create accounting entries or record manual off-system
handover of rewards. No automatic jobs/backfill, customer/provider payload changes,
refunds, bank transfers, invoices, fiscal receipts, notification calls or FE edits.
No rate default; changing policy does not change an existing financial snapshot.

Eligibility uses actual local canonical POS mono and Shop completion validators.
Shop completion authority is production, not payment authority provider/manual.
POS provider+https is required; simulation/test provenance is rejected. Tests use
synthetic PostgreSQL history and an intercepted HTTP client for the production POS
branch; guards remain intact and no real provider requests occur. Reward admission
itself consults no provider. Additional order discount uses existing fiscal net_lines
integer allocation, with deterministic remainder ties. ROUND_HALF_UP is once per line.

Locks follow existing inventory/returns: ALL item IDs ascending, ALL order IDs
ascending, then reward entries ascending. SQL and two-session tests prove a return
can acquire its order while accrual waits for its item, and committed dispute makes
a waiting close fail safely. Snapshots/audit/allocations are DB-immutable; only entry
review flag plus monotonic revision may change. Idempotency is private and actor-scoped.

Own history requires active account + current Master link, not new-sales/QR grants.
Pinned beneficiary ID prevents a reassigned account inheriting private history.
Admin management requires current active superuser. Shared/public DTOs are unchanged.
Private routes have no-store responses and input-free errors; Sentry drops private
request/error/trace/SQL breadcrumb data. Existing unrelated Sentry behavior is tested.

Return/dispute before transfer holds the entry; after transfer it adds needs_review
without reducing past payout, other entries, balance or creating debt. The final
post-payment return business rule is still pending. Existing known return cases hold
the whole order conservatively; this module does not implement POS/provider refunds.

## Checks actually run

Synthetic environment runner: `/tmp/run-master-rewards-tests.py`, Python
`/private/tmp/sc-payment-actions-venv-20261026/bin/python`. Only local PostgreSQL
127.0.0.1:55449 is allowed by a socket guard; all other socket destinations are denied.
No source .env or real credentials are used. Sentry uses an in-memory transport.

Targeted: test_master_rewards.py + test_master_rewards_privacy.py + test_sentry.py:
66 passed, 0 external network attempts, exit 0. Log `/tmp/master-rewards-tests.log`.

Combined: above plus test_master_mono_integration.py,
test_master_mono_live_integration.py, test_master_sales_integration.py,
test_shop_sales_integration.py: 131 passed, 0 external network attempts, exit 0.
Log `/tmp/master-rewards-regression.log`. Warnings are existing dependency/schema
warnings and SDK test context manager deprecation, not skipped/failed assertions.

Compilation and git diff --check passed. graphify update completed AST-only,
9480 nodes; oversized HTML visualization was automatically skipped. Graph output
is generated/local and excluded from the scoped source patch.

## Migration/release constraints

0144_master_rewards -> 0143_auto_checkout_checkpoint, additive six private tables.
Upgrade/downgrade/upgrade on empty history was exercised by the migrated template.
Downgrade refuses accepted private accounting history; explicit test preserves head
and ledger after refusal. Operational code rollback must leave private tables/data
in place; no automatic destructive migration rollback.

No commit, push, PR, release or production test performed by this task. The coordinator
reports independent review clean after the lock fix and a separate local integrated
FE/API/PostgreSQL check; these are coordinator evidence, not tests executed here.
Publication remains blocked pending explicit user authorization after auto-review
rejected an FE branch push. Do not bypass by publishing through another task/tool.
