# Review request analytics

## Cohort

`GET /api/v1/backoffice/reviews/metrics` uses one booking cohort throughout: completed bookings whose scheduled `Booking.start_at` is inside the inclusive Europe/Kyiv date range. Request, open, submitted-review, approval, rating, and moderation aggregates are joined back to those booking IDs.

The submission-date filters on the review moderation list are intentionally separate from this cohort.

## Persisted form opens

The existing request-context GET records a privacy-safe milestone in the same
request that successfully resolves and displays an available review form:

`GET /api/v1/public/reviews/request`

The client also retries the same idempotent write with:

`POST /api/v1/public/reviews/request/open`

The opaque token is supplied only in the `X-Review-Token` header. `review_form_open_events` has `UNIQUE(review_request_id)`, and PostgreSQL/SQLite inserts use `ON CONFLICT DO NOTHING`; reloads and retries therefore remain one opened request. A successful review submission inserts the same marker transactionally with source `submission_fallback` when the separate browser request was lost.

Metrics use `COUNT(DISTINCT review_request_id)`, not page loads.

Migration `0051_review_form_open_events` creates an initially empty
`analytics_tracking_markers` table. The first successful available-form GET,
POST retry, or review submission writes the open event and the
`review_form_opens` coverage marker atomically with one shared timestamp. This
keeps coverage conservative until a signal has actually been persisted instead
of treating schema rollout as proof that every application instance was
tracking. Coverage is evaluated from each request's link lifecycle, not from
the visit date:

- `available`: every sent request was sent after persisted tracking started (or the cohort has no sent requests);
- `unavailable`: every sent request had already expired before tracking started, so `review_form_opens` is `null`;
- `partial`: the cohort mixes fully tracked requests with older requests, or contains a link that was already active when tracking started. The returned open count is an observed lower bound.

Open-based rates are returned only for `available` coverage. Historical missing telemetry is never displayed as a real zero.

## Conversion definitions

- request → review: requests with both `sent_at` and `review_id` divided by requests with `sent_at`;
- request → open: sent requests with a persisted open divided by requests with `sent_at`;
- open → review: opened requests with `review_id` divided by opened requests.

The API also returns the exact intersection numerators (`sent_and_submitted_count`, `sent_and_opened_count`, and `opened_and_submitted_count`) so clients can validate percentages without incorrectly deriving them from marginal totals.

Provider delivery receipts are not used as the main denominator because receipt coverage may be incomplete. Reviews without a matching sent request are exposed as `submitted_without_sent_count` and excluded from the request-to-review percentage.

All rates are backend percentages in the `0..100` range. A missing denominator returns `null`, not `0`.

The `expired` metric is derived at query time from sent requests whose `expires_at` has passed and which have no review. It does not depend on the lazily updated request status, so links that were never reopened after expiry are still counted.
