# Automatic first-visit promotions

## Business behavior

The database promotion initially offers 20% off the eligible service subtotal on a customer's first completed barbershop visit. Eligibility belongs to the canonical customer across all masters. The booking customer resolver normalizes the supplied phone and treats Ukrainian local `050…`, legacy `+050…`, and international `+38050…` as aliases, then uses the existing email fallback. If legacy alias customer rows coexist, the exact supplied normalized phone is preferred, then the oldest alias row; imported history and reservations on every alias still apply. Existing customer records are not merged or rewritten. OTP verification follows the same exact-match preference and alias fallback so account access remains consistent. Completed bookings linked to that customer or canonical phone, imported visit dates, positive imported spending, and the durable completion marker disqualify returning customers. `imported_is_new_client=false` alone is not evidence of a visit: existing ordinary new customers have that default too.

The entitlement is reserved only when a first-visit promotion is actually selected. Confirmed and pending discounted bookings hold it; completion consumes it. Cancellation, no-show, and deletion of an editable booking release it. An inactive booking keeps its historic price/promotion snapshot, but editing it does not reserve anything. Reconfirmation checks eligibility and reserves again. Completion of any other visit also ends eligibility; a previously discounted booking must have its promotion explicitly removed by an administrator before it can subsequently be completed.

Reservation is transactional: identity-creation advisory locks serialize concurrent phone/email lookup, customer row locks (all alias rows, in ID order, before customer enrichment) serialize eligibility and completion, and a unique nullable `bookings.first_visit_customer_id` constraint prevents two reservations for one customer. A competing automatic booking can still be created at regular price by legacy callers omitting price approval; callers supplying a mismatched approved total are rejected. Quotes do not reserve anything.

No code, campaign membership, SMS delivery, or SMS link is needed. Existing booking notification behavior is unchanged by the promotion. No messaging provider is invoked by quotes or offer discovery.

## Promotion administration

Existing endpoints remain under `/api/v1/backoffice/promotions`: `GET`, `POST`, `GET /{id}`, `PATCH /{id}`, and `DELETE /{id}` (deactivation), with existing superuser authorization.

Added fields:

| Field | Values / meaning |
| --- | --- |
| `application_mode` | `code` (default for old records and requests), `automatic` |
| `eligibility_type` | Added `first_visit`; existing `all_customers`, `inactive_customers`, `military_customers` remain |
| `discount_percent` | Existing editable integer, 1–100 |

The existing `code` field remains a required unique internal identifier in administration, including automatic promotions. It is not a redeemable customer code when mode is `automatic`. Automatic military promotions are rejected because the existing military rule depends on code-based verification. Automatic inactive-customer promotions use the existing inactivity eligibility check.

Example new offer (the migration already creates this initial record):

```json
{
  "code": "FIRST_VISIT20",
  "name_uk": "Перший візит",
  "name_en": "First visit",
  "application_mode": "automatic",
  "eligibility_type": "first_visit",
  "discount_percent": 20,
  "applies_to_all_masters": true,
  "applies_to_all_services": true,
  "is_active": true,
  "is_public": true
}
```

Change the percentage with `PATCH /{id}` and `{"discount_percent": 25}`. Existing `master_ids`, `base_service_ids`, scope flags, localized names/descriptions, `starts_at`, `ends_at`, `is_public`, and `is_active` remain supported. Dates are inclusive and checked against the scheduled visit time; naïve timestamps use Europe/Kyiv. Use ISO timestamps with an explicit offset.

Confirmed bookings retain name, percentage, discount, total, and individual service-price snapshots when an administrator changes/disables the offer. Explicit booking service/price changes recompute using the stored percentage and retain authorized manual discounts. Rescheduling retains prices, but checks eligibility and current scope/date restrictions. An invalid change returns 409 and leaves the saved booking unchanged. Deactivation alone does not revoke an already confirmed promise. Explicit promotion selection replaces the prior promotion; an administrator can clear it with `PATCH /api/v1/backoffice/bookings/{id}` and `{"promotion_code": null}` without immediately reapplying automatic offers.

## Public offer discovery

`GET /api/v1/public/booking-promotions` lists active public catalog offers valid now. It returns terms, not customer eligibility. Existing master/service catalog responses also include these fields in `active_promotion`; their regular service price remains unchanged. The catalog picks the highest percentage, then lowest promotion ID, so use the list endpoint to discover all offers when several coexist.

```json
{
  "id": 123,
  "code": null,
  "name_uk": "Перший візит",
  "name_en": "First visit",
  "description_uk": "...",
  "description_en": "...",
  "discount_percent": 20,
  "application_mode": "automatic",
  "eligibility_type": "first_visit",
  "eligibility_scope": "barbershop",
  "requires_code": false,
  "conditional": true,
  "starts_at": null,
  "ends_at": null,
  "applies_to_all_masters": true,
  "master_ids": [],
  "applies_to_all_services": true,
  "base_service_ids": []
}
```

For other eligibility types `eligibility_scope` is null. Per-service `active_promotion` additionally contains `discount_amount` and `promotional_price`, which are conditional estimates, not guaranteed customer prices. Suggested copy: “20% off your first completed visit, subject to eligibility.” Use the returned percentage, never a frontend constant. No public offer response contains customer identifiers or history.

## Booking quote

`POST /api/v1/public/bookings/quote`

```json
{
  "master_id": 1,
  "service_ids": [10, 11],
  "start_at": "2026-10-06T12:00:00+03:00",
  "customer_phone": "+380501234567"
}
```

`service_id` may be supplied instead of `service_ids`. Phone and email are optional for browsing; provide the same customer details as the subsequent booking to check eligibility. `promotion_code` or `promotionCode` is optional. The quote validates active services/master and master redirection, but does not reserve the slot, validate all calendar availability, create customer records, send messages, or hold entitlement.

```json
{
  "subtotal_amount": 1500,
  "applied_promotion": {
    "id": 123,
    "code": null,
    "name_uk": "Перший візит",
    "name_en": "First visit",
    "discount_percent": 20,
    "application_mode": "automatic",
    "eligibility_type": "first_visit"
  },
  "discount_amount": 300,
  "total_amount": 1200,
  "eligibility": {
    "status": "applied",
    "explanation": "Promotion included; eligibility is checked again when booking."
  }
}
```

Amounts use the same integer price units as the existing booking API. Percentages round half up to a whole unit; only scoped services are discounted. A valid explicit code wins over automatic offers, even when smaller. Codes and automatic promotions do not stack. Among eligible automatic offers, the largest monetary discount wins, then the lowest promotion ID. Authorized manual discounts remain separate.

Eligibility status is `applied`, `customer_required`, or `not_available`. Unavailable quotes return regular subtotal/total, zero discount, and `applied_promotion: null`. Public explanations do not distinguish returning customers from other ineligible/unavailable cases and never return visit counts, dates, spending, or another booking ID. Treat quote results as provisional and render the actual booking response after creation.

Booking creation uses the existing `POST /api/v1/public/bookings` fields with no additional required input. Booking responses retain existing totals/promotion fields and add `promotion_application_mode_snapshot` and `promotion_eligibility_type_snapshot` (null for historic bookings without a promotion). Reservation IDs and durable customer eligibility markers are not exposed.

## Atomic price approval

`POST /api/v1/public/bookings` accepts optional `expected_total_amount`, a strict integer >= 0 in existing API price units. New clients send the exact `total_amount` of the approved, immediately reverified quote, including zero. Strings, booleans, fractions and negative values return 422. Omission (or null) preserves legacy behavior: server pricing is authoritative and may differ from an earlier quote.

The server independently computes service prices, promotion eligibility and entitlement reservation in the booking transaction. Before adding the booking, recording success, committing or scheduling booking notifications, it compares the computed total to `expected_total_amount`. A mismatch raises `409` with `detail: {"code": "price_changed", "message": "...", "total_amount": 1000}`. The transaction rolls back, including new customer creation and customer enrichment. No rejected booking, reservation or booking confirmation survives. The returned amount is informational, not a replacement quote: invalidate approval, call the quote endpoint again, and obtain fresh consent before retrying.

This is total-amount equality, not a signed quote, price override, offer/version guarantee, identity proof, reservation or idempotency key. Terms may change while the total remains equal. There is no token, expiry or replay protection; every attempt recomputes pricing and checks normal booking constraints. Promotion edits after price calculation do not rewrite that booking's saved snapshot. Existing validation and slot conflicts can still reject a request before the price comparison.

## Errors and lifecycle changes

| HTTP / state | Meaning / frontend action |
| --- | --- |
| 200 `customer_required` | Ask for customer details before showing a confirmed discount |
| 200 `not_available` | Show regular price; no customer history is disclosed |
| 400 | Existing invalid/inactive/expired/private code, unsupported configuration, or service-scope error; show returned validation message |
| 409 `detail.code=entitlement_reserved` | First-visit entitlement unavailable for this requested mutation; refresh quote/booking |
| 409 `detail.code=not_eligible` | First-visit promotion unavailable; administrator can explicitly remove it |
| 409 `detail.code=promotion_no_longer_applicable` | Changed date/services violate offer terms; saved booking is unchanged |
| 409 `detail.code=promotion_unavailable` | Referenced promotion is missing; select/remove it explicitly |
| 409 `detail.code=customer_required` | Cannot preserve first-visit offer without a canonical customer |
| 409 `detail.code=price_changed` | Approved total differs from transaction pricing; no booking/confirmation; requote and request fresh approval |
| 422 | Request validation, including malformed phones/service IDs |

The existing Backoffice status endpoints accept the added `no_show` status. It does not count as a completed visit and releases entitlement. Rescheduling uses existing booking PATCH endpoints. Reconfirmation after cancellation/no-show is an explicit status change and is revalidated. Completed bookings remain protected against schedule changes; existing superuser price corrections remain possible using their consumed promotion snapshot.

## Migration and verification

Apply `alembic upgrade head` using the repository's normal migration process. Revision `0071_first_visit_promotions` adds enum values, application mode, booking reservation/snapshot fields, and the durable customer marker; backfills known completed/imported dates and legacy code-mode snapshots; inserts the initial offer with `ON CONFLICT (code) DO NOTHING`. It never overwrites an existing identifier or later administrator changes. No startup seed resets configuration. Rollback deactivates automatic promotions and maps no-show to cancelled; PostgreSQL enum labels remain.

Tests use in-memory provider doubles and a dedicated local test database with isolated schemas. Opt in to database tests with `SEGMENTS_TEST_DATABASE_URL` pointing to a localhost database whose name contains `test`; it never defaults to the application database. Run `python3 -m pytest -q`. The new integration suite is `tests/test_first_visit_integration.py`.
