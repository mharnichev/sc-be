# Booking availability and alternatives performance

## Scope and compatibility

This optimization changes read-side work in `BookingServiceLayer` and `BookingAlternativesService`. The HTTP alternatives endpoint calls `find_page`; `find` remains a complete-result service entry point. Public request/response schemas are unchanged. Booking creation, quote validation, canonical-calendar locking, rescheduling, repeat booking, waitlist claims, and analytics transaction boundaries remain authoritative.

The starting working tree already merged touching/overlapping published windows within a Kyiv working day. It offered the start of each free interval, including starts off the grid, plus the fixed 15-minute grid anchored to the working day. Those rules are preserved. Intervals are half-open; touching bookings do not overlap. Monday closure, 09:30–20:00 hours, the two-calendar-month horizon, strict future starts, active catalog validation, and live holds continue to apply.

Public source services determine duration and public service IDs. Redirected calendars determine the physical resource and mapped catalog price. Hidden technical calendars never become public alternatives. Candidates with invalid redirect/service mappings are omitted; invalid requested selections still fail validation.

Browser verification also reproduced a mobile confirmation-button obstruction: partially moving the floating action group offscreen left its container and buttons over the form. `FloatingBookingDrawer.vue` now hides the complete group while its stable bounding rectangle overlaps the booking section. Hidden controls do not intercept pointer or keyboard interaction; scrolling away restores the group and its drawer entry point.

Read requests also use an explicit deadline with a separate child AbortController. The installed ofetch 1.5.1 ignores its timeout option when given an external signal. The wrapper rejects at 15 seconds for slots and 20 seconds for alternatives/date ranges, cancels the transport, preserves the parent request generation so the error is displayed, and removes its timer/listener after settlement. Explicit context cancellation still invalidates old results; reads disable implicit retries and expose a deliberate retry to the user. Browser tests reproduced the original hanging state before this fix.

## Work reduction

- Alternatives resolve candidate metadata once per request and reuse loaded redirect targets. Calendar windows, active bookings, unexpired active holds, and blocks are loaded sequentially in batches of at most 256 distinct physical calendar IDs: four calendar queries per batch. Catalog and redirect queries are additional. No concurrent queries run on a shared `AsyncSession`.
- Immutable request-local interval maps group facts by physical calendar and Kyiv date. Slot calculations scan relevant daily intervals rather than the entire horizon's occupied intervals.
- Date availability uses the same slot kernel with an existence mode. It stops after the first valid start and creates no slot response models. A range containing only closed days still validates the requested catalog and duration, then avoids calendar reads.
- Alternatives retain compact internal offers for ranking and exact counting. Only offers on the requested page become public response models and receive opaque offer IDs. Recommendation diversity, physical-slot deduplication, ordering, context/offer IDs, and exact `total_count` remain unchanged.

Exact counts and globally diverse recommendations still require examining the whole eligible set. Offset pagination therefore recomputes live availability on every page. CPU and memory are still proportional to eligible candidates; this change does not introduce a constant-time cursor, approximate counts, horizon truncation, or a cross-request cache. Shared calendars are loaded once, but different source service durations still need their own calculation.

## Changes between pages

Pagination represents current availability, not a reserved snapshot. A booking, cancellation, hold expiry, catalog edit, or schedule edit between requests can shift offsets, counts, and page membership. Stable context IDs identify the selection, not a database revision. A refreshed search is appropriate when consistency across changed pages matters. Quote and final booking validate current state; displaying an offer does not reserve it. Client request cancellation does not prove server-side work or transactions have stopped.

## Verification and reproduction

Task evidence is under `output/booking-optimization/`, with source identities in `baseline-manifest.json`, the requirement matrix in `acceptance-matrix.md`, preserved failed runs, and final results in the task report. Compare the captured dirty starting tree, not Git HEAD: unrelated and preceding booking changes existed at task start.

The independent snapshot comparison uses a fixed random seed and compares complete responses and first/middle/final/out-of-range pages against the actual starting implementation. Permanent tests also assert bounded model allocation, fresh busy-state reads, redirect identity/duration/price, exhaustive pagination, a brute-force interval oracle, existence equivalence, validation before short circuits, and bounded calendar queries beyond one batch.

The captured source is archived as `baseline-source.tar.gz`; comparison helpers extract it into their own temporary directory and clean it up afterward, keeping duplicate tests out of ordinary discovery. Run backend tests with `pytest -q tests`. PostgreSQL tests require their explicit disposable-database opt-in; an ordinary unit run does not prove transaction concurrency. Browser tests require the opt-in isolated stack, synthetic fixtures, disabled notification providers, and cleanup that fails on errors. Never direct these tests or load generators at production.

## Deployment and rollback

No new migration, index, external service, or cache is introduced by this optimization. Existing uncommitted migrations from earlier work are outside its rollback boundary. Deploy through the normal reviewed release process only after required acceptance gates pass. Keep frontend/API contracts together with the existing booking recovery release.

Rollback this optimization's service and route changes together to the recorded starting snapshot; do not discard unrelated work or roll back earlier schema changes. There is no cache invalidation or data backfill. Re-run available-slots, date range, alternatives pagination, quote, and booking smoke tests after rollout or rollback. This task does not deploy either action.

## Release monitoring

Measure endpoint latency distributions separately for available-slots, date ranges, first/subsequent alternative pages, quote, and creation. Track SQL count/time, pool wait and active connections, application CPU/RSS, eligible offer count versus returned count, response bytes, 4xx/5xx rates, booking conflicts, and analytics failures. Segment by horizon length, public/physical master count, and occupied interval count; avoid customer data and high-cardinality IDs in metric labels.

Set alerts from observed production baselines and product SLOs, including sustained p95/p99 regressions, pool saturation, error bursts, and unexpected growth in empty results or conflicts. Diagnose correctness regressions independently of latency. Local benchmarks cannot establish production capacity or conversion impact.
