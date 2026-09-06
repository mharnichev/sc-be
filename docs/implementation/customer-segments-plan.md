# Customer segments implementation

## Plan and ownership

1. Inspect messaging/history/import/promotion behavior and settle API contract (primary agent).
2. Implement validated segment rules, saved models and SQL evaluator (segments agent; new segment model/schema/service/tests).
3. Implement immutable campaign runs and delivery safeguards (campaigns agent; messaging model/schema/service and new campaign-run files).
4. Integrate authenticated routes, migration and frontend contract (primary agent).
5. Exercise isolated database + sandbox delivery; independent correctness, migration, authorization, concurrency and performance review (verification agent, primary integration).
6. Resolve findings, run regression tests and update graphify (primary agent).

## Decisions

- Membership is independent from contact eligibility. Segments only select customers; campaigns govern delivery.
- Existing inline filters remain supported with their historical semantics; segment audiences use the new authoritative completed-visit evaluator.
- No promotion eligibility code changes. Current promotions combine completed Booking.end_at and imported_last_visit_at, use fixed day cutoffs, and permit missing history. New inactivity segments must require known history.
- Draft creation never sends. Scheduled segment runs resolve membership when due, at a single recorded timestamp; run membership, segment revisions and message configuration remain historical facts.
- All work uses fake providers and disposable local data. No production campaigns, migrations, deployments or customer messages.

## Progress

- Initial graph query and source inspection complete. Unrelated working-tree review changes identified and preserved.
- Segment and campaign implementation, authenticated APIs, scheduler configuration and migration complete.
- Frontend contract and representative payloads finalized in docs/customer-segments-api.md.
- Independent review findings resolved, including SQL materialization/CTE collisions and concurrent worker/deletion lock races.
- Final full pytest run with isolated PostgreSQL: 622 passed, no skips or failures; 13 existing deprecation warnings.
- Authenticated HTTP sandbox flow, actual migration round-trip, repeatable snapshot pages and repeated concurrency checks passed.
- graphify update . completed (AST only, no API cost): 3,667 nodes and 13,190 edges.
- No production/staging deployment or live-provider checks; scheduler remains disabled by default.
