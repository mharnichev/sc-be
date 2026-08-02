# Booking funnel observability

## Public booking frontend

Generate a new cryptographically random anonymous session ID for each booking attempt and keep it for the life of that attempt. The Soulcuts frontend shares the attempt between the embedded form and drawer, persists it in `sessionStorage` with a two-hour inactivity TTL, and removes it after a successful booking. Reuse a stable event ID when retrying or backfilling the same event.

`POST /api/v1/public/booking-funnel/events`

```json
{
  "event_id": "evt-01HZY7QX6FD5",
  "anonymous_session_id": "session-01HZY7QX6FD5Q9BN",
  "event_type": "service_selected",
  "master_id": 7,
  "service_id": 11
}
```

Allowed client event types:

- `booking_start`
- `service_selected`
- `master_selected`
- `slot_selected`
- `contact_entered`
- `no_slot`
- `stale_schedule`
- `booking_error`

When a successful availability request returns no slots for an otherwise open/selectable day, send the searched Kyiv calendar date and the complete selected service set:

```json
{
  "event_id": "evt-01HZY7QX6FD6",
  "anonymous_session_id": "session-01HZY7QX6FD5Q9BN",
  "event_type": "no_slot",
  "master_id": 7,
  "service_id": 11,
  "service_ids": [11, 12],
  "target_date": "2026-08-08"
}
```

`service_id` remains the primary selected service for compatibility. `service_ids` is normalized and validated as a set of at most ten active services belonging to `master_id`. Network/API failures, explicitly closed dates, and stale-slot booking conflicts are different signals and must not be sent as `no_slot`.

The endpoint returns HTTP 202:

```json
{
  "event_id": "evt-01HZY7QX6FD5",
  "status": "recorded"
}
```

An already accepted event ID returns `"status": "duplicate"`. The server stores keyed hashes of both the event ID and anonymous session ID. Arbitrary metadata is rejected; contact details, comments, request bodies, IP addresses, and message contents are not persisted in funnel tables.

Client-provided event IDs and server-generated booking milestones use separate HMAC namespaces, so even a deliberately chosen public ID cannot collide with a server success and roll back a valid booking.

Do not submit `booking_success` to this endpoint. Include the same anonymous attempt ID in the existing booking request:

`POST /api/v1/public/bookings`

```json
{
  "master_id": 7,
  "service_id": 11,
  "customer_name": "Customer",
  "customer_phone": "+380501112233",
  "start_at": "2026-07-25T12:00:00+03:00",
  "funnel_session_id": "session-01HZY7QX6FD5Q9BN"
}
```

`funnelSessionId` is also accepted. A `booking_success` event is inserted by the server in the same database transaction as the booking. When a session ID is present, the server also inserts inferred prerequisite milestones for that same session. These rows make a successful booking resilient to lost best-effort browser events without inflating counts because aggregation deduplicates by session.

The public HTTP route records a server success even when an old or blocked client omits `funnel_session_id`; that row has no anonymous session and is reported as `unattributed_booking_successes`, never included in a conversion percentage. Backoffice and Telegram-created bookings do not enter the public web funnel.

For redirected bookings, funnel `master_id` consistently means the master selected by the visitor. The actual fulfilment master remains available on the booking domain model.

## Backoffice owner dashboard

The existing owner endpoint remains:

`GET /api/v1/backoffice/statistics/admin/dashboard?date_from=2026-07-01&date_to=2026-07-31&compare_to_previous=true`

Its response includes `booking_funnel`. The selected inclusive Europe/Kyiv dates define the cohort from each attempt's earliest persisted `booking_start`, with a half-open database boundary. Contextual backfills therefore cannot place one attempt in multiple date cohorts. Later events from the same anonymous attempt are allowed to mature after the end boundary:

- `status`: `available`, `partial`, `empty`, or `unavailable`
- `calculation_version`: currently `2`
- `steps`: distinct anonymous-session counts for every step, including `booking_success`
- `step_to_step_conversion`
- `overall_conversion`
- `drop_offs`
- `tracking_gap_count`: destination session-transition pairs missing the preceding event
- `unattributed_booking_successes`: public server successes excluded from percentages because no session was supplied
- `operational_alerts`: `no_slot`, `stale_schedule`, and `booking_error` counts/rates and trigger state
- `alert_thresholds`
- `no_slot_dates`: day-level totals for selected dates that returned no available slots
- `no_slot_contexts`: exact `target_date` + selected master + complete service-set breakdown, with current display names, idempotent observations, unique anonymous sessions, and first/last observation time
- `no_slot_unknown_date_count`: legacy observations for which the selected date was not stored and cannot be reconstructed
- `no_slot_context_limit` and `no_slot_contexts_truncated`: deterministic response cap and explicit truncation state
- `weekly_insight_uk`
- `recommended_action`: one deterministic action based on the strongest meaningful signal; operational alerts are ranked by how far they exceed their own configured thresholds, while funnel transitions are ranked by drop-off rate
- `latest_weekly_digest`: latest persisted all-master Monday–Sunday digest, or `null`

For every transition `A → B`, the denominator is the set of sessions with `A` and the numerator is the intersection of sessions with both `A` and `B`. Overall conversion is `booking_start ∩ booking_success` divided by `booking_start`; independent marginal counts are never divided. An empty period returns `status: "empty"` with empty metric arrays rather than invented conversion values. Missing baselines and orphan steps return explicit unavailable/partial states with reasons.

When a master filter is active, `booking_start` and `service_selected` rows without a master are attributed to a master selected later in the same attempt. Later steps require that master directly. A visitor may evaluate multiple masters in one attempt, so per-master funnel counts are intentionally not additive.

Operational `no_slot`, `stale_schedule`, and `booking_error` counts remain scoped to events observed inside the selected period. Their counts are distinct affected sessions; the no-slot rate uses the intersection with master-selected sessions. The date table separately exposes both idempotent observations and unique sessions.

The dashboard period filters `occurred_at` (when Soul Cuts observed the unsuccessful search), not `target_date` (the day the visitor wanted to book). Consequently a searched date may be later than the selected dashboard period. Context rows are ordered by searched date descending and capped at 250; `no_slot_contexts_truncated=true` means the UI is showing only that deterministic first page. Deleted masters or services keep their identifiers but may have a `null` display name. No customer PII is stored or returned.

## Migration and configuration

Apply all funnel migrations through the current head:

```shell
alembic upgrade head
```

Configuration variables and defaults:

- `BOOKING_FUNNEL_HASH_SECRET`: optional dedicated HMAC secret; falls back to `SECRET_KEY`
- `BOOKING_FUNNEL_EVENT_RATE_LIMIT=120`: per-process events per minute per privacy-safe client/session key
- `BOOKING_FUNNEL_DIGEST_SCHEDULER_ENABLED=true`
- `BOOKING_FUNNEL_DIGEST_SCHEDULER_INTERVAL_SECONDS=3600`
- `BOOKING_FUNNEL_NO_SLOT_ALERT_MIN_COUNT=3`
- `BOOKING_FUNNEL_NO_SLOT_ALERT_RATE_PERCENT=20`
- `BOOKING_FUNNEL_STALE_SCHEDULE_ALERT_COUNT=1`
- `BOOKING_FUNNEL_ERROR_ALERT_COUNT=1`
- `BOOKING_FUNNEL_MEANINGFUL_STEP_SESSIONS=5`

The scheduler follows the existing FastAPI lifespan task pattern and uses a PostgreSQL transaction advisory lock plus a unique digest-period constraint. It recalculates the one row for the latest completed week on every scheduler run during the following week, so attempts that started near Sunday midnight can mature without making the live funnel and persisted digest disagree.
