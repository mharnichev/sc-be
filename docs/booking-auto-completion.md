# Automatic booking completion

Confirmed bookings finish automatically after the workday closes at 20:00
Europe/Kyiv. Pending, cancelled, no-show, already completed and future bookings
are excluded. Mark cancellations/no-shows before closing to keep them excluded.

The lifespan worker runs on startup and every 60 seconds. It also catches up
closed workdays after downtime, but only for bookings starting on or after
`BOOKING_AUTO_COMPLETION_STARTS_ON` (default: 2026-10-11, this feature's introduction).
This prevents deployment from completing the historical backlog. Set the date
explicitly to the rollout date if activating the feature later. Disable the
worker with `BOOKING_AUTO_COMPLETION_ENABLED=false`; the interval can be set
with `BOOKING_AUTO_COMPLETION_INTERVAL_SECONDS` (10–3600).

Each visit is rechecked under a PostgreSQL row lock shared with manual booking
updates. Concurrent workers skip locked visits. Each completion commits
independently; a promotion conflict rolls back that visit, logs its ID, and
does not stop other visits. Failed visits retry on the next iteration.
Promotion/first-visit state and older repeat-booking offers follow the same
completion operations as the manual status route. `completed_at` records the
processing time, with microsecond ordering inside a batch. This keeps recovered
visits discoverable by the review scheduler. Repeat-offer eligibility, link
validation and supersession instead order visits by `start_at`, with `id` as the
tie-breaker, so an older visit retried later cannot supersede a newer visit.
Manual completion continues to record the processing time. Repeat-offer cadence
still uses `completed_at`, and contact frequency limits are unchanged.

The worker does not send messages. The existing review scheduler creates requests
and schedules them for the next day at the configured send time (default: 10:00).
Late recovery after that time schedules the next morning. Review SMS dispatch,
including retries and Telegram fallback, is always restricted to 10:00–20:00
Kyiv time, even when optional campaign quiet hours are disabled. Existing consent,
exclusions and review frequency limits still apply; completion alone does not
guarantee a request is sent. No migrations are needed. Production must receive
the new backend image for this behavior to become active.
