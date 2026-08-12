# Telegram one-tap repeat booking

The backend sends an eligible customer a Telegram-only offer after a completed visit. The link is a hash-only, expiring capability in the URL fragment; it never creates a booking. The normal booking endpoint remains responsible for current availability and working-hours validation.

## Public frontend contract (`sc-fe/apps/barbershop`)

The button opens `${PUBLIC_SITE_URL}${REPEAT_BOOKING_PUBLIC_PATH}#<opaque-token>`. The frontend must read the fragment locally, remove it from the visible URL with `history.replaceState`, and send it only in the `X-Repeat-Booking-Token` header. It must not copy the token into query strings, analytics, logs, or error reporting.

1. `GET /api/v1/public/repeat-booking/context` with `X-Repeat-Booking-Token` resolves the minimum prefill context and records the first open.
2. `POST /api/v1/public/repeat-booking/start` with the same header records that the client entered the time-selection flow and returns the same context.
3. Use the existing availability endpoint with the returned master/service IDs. The client must select a date and time.
4. Submit the existing `POST /api/v1/public/bookings` request with `X-Repeat-Booking-Token`. Availability is validated again in the normal booking transaction, then the capability is consumed.

Context response:

```json
{
  "preferred_master": {"id": 7, "name": "Олег", "available": true},
  "services": [
    {"id": 31, "name": "Чоловіча стрижка", "available": true},
    {"id": 32, "name": "Борода", "available": true}
  ],
  "can_prefill": true,
  "fallback_required": false,
  "expires_at": "2026-10-08T07:00:00Z"
}
```

If the old master or any exact service is no longer public, `can_prefill` is false and `fallback_required` is true. The frontend should show the existing public master/service catalog and let the client choose alternatives. No customer, booking, phone, Telegram, or raw internal reference is returned.

All capability responses use `Cache-Control: no-store, private`. Invalid, expired, revoked, consumed, or superseded capabilities return `401`.

## Policy and delivery

- Default cadence: 28 days after `completed_at`; `REPEAT_BOOKING_SERVICE_DELAY_DAYS` can override individual barber-service IDs. A multi-service visit uses the longest applicable cadence.
- Frequency cap: one sent offer per customer in 30 days.
- Token lifetime: 30 days.
- Quiet hours: 20:00–10:00 in `Europe/Kyiv`.
- Delivery: Telegram only, with the inline button `Записатися знову`; there is no SMS fallback.
- Type-specific opt-out: `client_communication_preferences.repeat_booking_opt_out`, in addition to existing marketing consent, do-not-contact, and blacklist checks.

Backoffice can read `GET /api/v1/backoffice/repeat-booking/analytics?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD` for the sent → opened → started → completed-visit funnel and safe skip reason counts.
