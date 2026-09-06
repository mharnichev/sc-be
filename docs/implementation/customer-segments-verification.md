# Customer segments verification record

## Scope and environment

All database checks use the disposable local PostgreSQL 16 container `soulcuts-segments-test-20260906` on loopback port 55439, database `segments_test`, with a unique temporary schema per test. Tests refuse remote/non-test database names and never fall back to the application's configured database. Providers record messages in memory; no real SMS or Telegram messages are sent. HTTP tests use the actual FastAPI routes and admin JWT authentication without starting application background schedulers.

## Independent review and resolutions

- Status model/migration mismatch: segment lifecycle uses a non-native enum with matching varchar storage.
- Snapshot consistency across pages: repeatable-read transactions with bounded serialization/deadlock retries and locked campaign/segment configuration.
- Async lazy-load after worker refresh: worker explicitly refreshes the campaign/preference data it uses.
- Runs containing only skipped/failed recipients: terminal outcomes now feed run completion; completion is serialized across workers.
- Unknown provider acceptance and worker interruption: durable reservation before I/O; no automatic retry/fallback of uncertain sends, and stale reservations become observable failures.
- Customer deletion could remove imported-only audience history: existing history-based customer deactivation includes run recipients. Campaign deletion also archives when runs exist and locks against concurrent launch.
- Historical marketing classification: run contact history uses frozen campaign purpose after campaign edits.
- Migration/query index drift: completed/upcoming history and contact indexes are represented in metadata and migration.

## Checks performed

- Pure/date/validation segmentation tests, existing messaging/recipient API/promotions/import tests.
- Full repository pytest suite, with explicit PostgreSQL integration suite run separately.
- Real completed/cancelled/imported/unknown history, calendar boundaries, ALL/ANY/exclusions and stable multi-service catalog IDs.
- Repeated and concurrent launches; overlapping segments deduplicated; immutable segment revision/rules/rendered message snapshots.
- Consent changes after snapshot, concurrent recipient attempts and cross-campaign marketing limits.
- Ambiguous provider errors produce one attempted provider call and no retry or other-channel send.
- Authenticated HTTP creation → preview → draft (zero recipients) → run snapshot → sandbox worker → result inspection; segment update conflicts and archive history.
- Actual feature migration downgrade/upgrade/re-upgrade on disposable PostgreSQL, preserving an existing legacy recipient.

Final full-suite command:

```sh
SEGMENTS_TEST_DATABASE_URL='postgresql+asyncpg://segments_test:segments_test@127.0.0.1:55439/segments_test' python3 -m pytest -q
```

Result: **622 passed, no failures or skips, 13 existing deprecation warnings, 25.02 seconds**. Includes 17 real PostgreSQL tests (13 general + 4 history/scheduling). Compilation and git diff whitespace checks passed. Final graphify AST update completed with 3,667 nodes and 13,190 edges.

## Boundaries

This is a local PostgreSQL + in-memory-provider integration proof, not proof of live provider behavior or staging deployment. Production migrations, deployments, real campaigns and real customer messages were not run. Graphify vault exists locally; opening its Obsidian URL was unavailable because no application registered the URL scheme.

## Measured query optimization

A representative disposable dataset contained 5,000 customers and 50,000 completed visits. EXPLAIN ANALYZE exposed a PostgreSQL nested-loop plan that repeated the grouped history scan per outer customer for inactivity + visit-count + upcoming-booking rules. Materializing the matched-ID CTE eliminated that repeated work: the same query fell from 58,178.94 ms to 72.709 ms (about 800× in this synthetic local check). The completed-visit scan ran once; bounded explanations for 100 customers took 1.992 ms and visited 1,000 relevant visit rows. These are local fixture measurements, not production latency guarantees.

## Additional compatibility checks

Existing inline birthday/follow-up/loyalty campaigns retain manual launch support. Service-event/master legacy start calls retain their previous enqueue path. New marketing caps do not intercept service notifications even when older rows still use the default marketing purpose. Pre-send upcoming bookings and completed returns, unauthorized customer/master tokens, stale send reservations and history-preserving customer deactivation are explicitly covered.

## Final concurrency and compilation review

The final whole-suite run uncovered anonymous CTE name reuse when multiple identical segments were compiled together. Explicit unique CTE namespaces eliminate the collision; a repeated 20-overlapping-segment compilation regression covers it. This reduces compiled-statement-cache reuse but retains the measured materialized-query performance (final benchmark 72.123 ms, 100-member facts 2.382 ms).

Repeated workers uncovered a PostgreSQL foreign-key lock cycle between delivery logs and customer reservations. Reservations now use `FOR NO KEY UPDATE`, preserving customer serialization while allowing foreign-key `KEY SHARE`; the concurrency test passed 12 consecutive isolated database executions. A separate snapshot/deletion test deliberately observes a PostgreSQL lock wait, then confirms customer deactivation and retained snapshot membership after snapshot commit.

The disposable test container was stopped after verification completed.
