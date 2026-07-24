# Booking funnel observability

## Public booking frontend

Generate a new cryptographically random anonymous session ID for each booking attempt and keep it for the life of that attempt. Reuse a stable event ID when retrying the same event.

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

The endpoint returns HTTP 202:

```json
{
  "event_id": "evt-01HZY7QX6FD5",
  "status": "recorded"
}
```

An already accepted event ID returns `"status": "duplicate"`. The server stores keyed hashes of both the event ID and anonymous session ID. Arbitrary metadata is rejected; contact details, comments, request bodies, IP addresses, and message contents are not persisted in funnel tables.

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

`funnelSessionId` is also accepted. A `booking_success` event is inserted by the server in the same database transaction as the booking. Existing callers that omit the new optional field retain their current booking behaviour and are not treated as web-funnel attempts.

## Backoffice owner dashboard

The existing owner endpoint remains:

`GET /api/v1/backoffice/statistics/admin/dashboard?date_from=2026-07-01&date_to=2026-07-31&compare_to_previous=true`

Its response now includes `booking_funnel`, using the same inclusive Europe/Kyiv calendar dates and half-open database boundary as the rest of the dashboard:

- `status`: `available`, `partial`, `empty`, or `unavailable`
- `steps`: distinct anonymous-session counts for browser steps and distinct real booking counts for `booking_success`
- `step_to_step_conversion`
- `overall_conversion`
- `drop_offs`
- `operational_alerts`: `no_slot`, `stale_schedule`, and `booking_error` counts/rates and trigger state
- `alert_thresholds`
- `weekly_insight_uk`
- `recommended_action`: one deterministic action based on the strongest meaningful signal
- `latest_weekly_digest`: latest persisted all-master Monday–Sunday digest, or `null`

An empty period returns `status: "empty"` with empty metric arrays rather than invented conversion values. Missing baselines or non-monotonic tracking returns explicit unavailable/partial metrics with reasons.

## Migration and configuration

Apply migration `0042_booking_funnel`:

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

The scheduler follows the existing FastAPI lifespan task pattern, uses a PostgreSQL transaction advisory lock plus a unique digest-period constraint, and logs created, already-existing, lock-skipped, and failed iterations.
