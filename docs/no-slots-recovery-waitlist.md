# No-slots recovery and waitlist API

All calendar dates and slot timestamps are interpreted in `Europe/Kyiv`. Public
responses never contain a customer phone number, database customer ID, waitlist
database ID, or plaintext token stored by the backend.

## Booking alternatives

`POST /api/v1/public/booking-alternatives`

```json
{
  "master_id": 7,
  "service_ids": [11, 12],
  "desired_date": "2026-08-08",
  "duration_minutes": 90,
  "another_master_acceptable": true,
  "funnel_session_id": "anonymous-session-123456"
}
```

The response contains `same_master` and `other_masters`. Each item contains the
public master `id`, `name`, photo/avatar, localized role and rating when the
domain provides it, plus `start_at`, `end_at`, `date` and `duration_minutes`.
The same-master group contains up to three nearest slots after the requested
date. Other-master results prefer the requested date and otherwise the nearest
date within the normal booking horizon. Empty arrays are a successful response.

The service uses the same availability windows, closed days, active-service
checks, time blocks, bookings, waitlist holds, work hours and 15-minute slot
step as real booking creation. Other masters must be active, public and able to
perform every selected service (catalog-equivalent per-master service IDs are
resolved on the server).

When a user selects an alternative, send:

`POST /api/v1/public/booking-recovery/events`

```json
{
  "event_id": "alternative-choice-unique-id",
  "anonymous_session_id": "anonymous-session-123456",
  "event_type": "alternative_slot_selected",
  "master_id": 9,
  "service_id": 21
}
```

Then create the normal booking with the chosen master's service IDs, the same
`funnel_session_id`, and `"recoverySource": "alternative"`.

## Waitlist request and cancellation

`POST /api/v1/public/waitlist`

```json
{
  "customer_name": "Іван Петренко",
  "customer_phone": "+380671234567",
  "service_ids": [11, 12],
  "preferred_master_id": 7,
  "desired_date": "2026-08-08",
  "acceptable_date_from": "2026-08-08",
  "acceptable_date_to": "2026-08-10",
  "preferred_time_from": "10:00:00",
  "preferred_time_to": "15:00:00",
  "duration_minutes": 90,
  "notification_consent": true
}
```

`preferred_master_id` is nullable for any suitable public master. Consent must
be explicitly true. The response contains only `public_id`, status,
`expires_at`, and a one-time opaque `cancel_token`. Equivalent open requests
return `409`. No booking is created.

Requests expire immediately after the final acceptable Kyiv date, with the
configured 90-day maximum as a safety cap. Statuses are `active`, `offered`,
`booked`, `expired`, and `cancelled`.

To cancel an active or offered request:

`POST /api/v1/public/waitlist/cancel`

```json
{"cancel_token": "opaque-token-from-create-response"}
```

The token is HMAC-hashed at rest. Reuse after cancellation returns `409`.

The frontend should also record opening the form through the recovery event
endpoint with `event_type: "waitlist_opened"` and a unique `event_id`.

## Offer and claim

Cancellation, rescheduling and deletion of a confirmed booking enqueue the
freed interval for matching. Ranking is:

1. exact preferred master;
2. exact desired date, then requested time preference;
3. oldest request, then stable request ID.

Only one request receives an offer for a slot at a time. Communication consent,
transactional opt-out/blacklist, quiet hours, a per-customer frequency cap,
duplicate offers, conflicting bookings, service equivalence, duration and date/
time range are checked before SMS delivery.

The default hold is 10 minutes (`WAITLIST_OFFER_HOLD_MINUTES`). Sent/delivered
holds are excluded from normal availability and booking creation. The claim
endpoint locks the offer and master, then rechecks availability before creating
the booking and closing the offer/request in one transaction:

`POST /api/v1/public/waitlist/offers/claim`

```json
{"token": "opaque-token-from-the-link-fragment"}
```

A claimed, expired or otherwise reused token returns `410`. An occupied slot
returns `409`; it is never silently double-booked. Expired or failed offers are
passed to the next eligible request by the scheduler.

The SMS template (`WAITLIST_OFFER_SMS_TEMPLATE`) supports:

- `{master_name}`
- `{appointment_date}`
- `{appointment_time}`
- `{hold_minutes}`
- `{booking_link}`

The default link opens
`{PUBLIC_SITE_URL}{WAITLIST_OFFER_PUBLIC_PATH}#...`. Keeping the token in the
URL fragment prevents it from reaching CDN and web-server access logs. The barbershop
frontend must render that route, explain that the slot is held but not booked,
and send the token in the claim request body after explicit confirmation.

## Analytics

Client-owned `alternative_slot_selected` and `waitlist_opened` events use the
public recovery event endpoint. Requested/returned alternatives, waitlist
submission, offer sent/delivered/claimed/expired and bookings after alternative
or waitlist recovery are recorded by the server. Anonymous session identifiers
and idempotency keys are keyed hashes; phone numbers are not stored in events.

Admins can query:

`GET /api/v1/backoffice/booking-recovery/summary?date_from=2026-08-01&date_to=2026-08-31`

It returns no-slot sessions, alternative response/selection/recovery metrics,
waitlist requests, offer delivery/claim/expiry counters, cancelled slots
refilled and average cancellation-to-refill seconds.
