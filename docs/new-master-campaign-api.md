# New Master campaign contract for sc-fe

All routes below are under `/api/v1`. Back-office routes require the existing admin bearer session. Public offer tokens are opaque entitlements and never customer login credentials. Do not render or log the full token in analytics, error reporting, or third-party URLs. Preserve it in the booking form state/session storage across reloads; send it only to the offer/quote/booking APIs. The frontend booking route is `/booking?offer_token=<43-character-token>`.

## Campaign configuration

Use the existing `POST /backoffice/messaging/campaigns` and `PATCH /backoffice/messaging/campaigns/{id}`. Template text uses existing editable `/backoffice/messaging/templates` endpoints. Segment selection uses existing `/backoffice/segments` endpoints.

```json
{
  "name": "New Master — Andrii",
  "type": "re_engagement",
  "status": "draft",
  "channel": "sms",
  "purpose": "marketing",
  "timezone": "Europe/Kyiv",
  "template_id": 12,
  "segment_ids": [7],
  "offer_master_id": 4,
  "offer_promotion_id": 5,
  "offer_service_ids": [18, 19],
  "master_name_for_message": "Андрія Віканова",
  "sending_window": {"start": "10:00", "end": "18:00", "days": [0,1,2,3,4,5,6]},
  "sms_recipients_per_minute": 20,
  "marketing_frequency_days": 7,
  "marketing_max_contacts": 1,
  "marketing_cap_days": 7
}
```

IDs are examples except verified initial seed master 4; load current promotion/template/segment/service IDs. This request intentionally has no validity dates and remains a draft. Administrators must set both `offer_starts_at` and `offer_expires_at` as ISO 8601 timestamps with UTC offsets. No dates are invented. Offer visits must begin in `[starts_at, expires_at)`, and new bookings must also be created while the offer is valid.

The selected master/promotion have foreign keys. Service IDs explicitly reference `BarberService` records belonging to the selected active master. Select normal active services from that master's catalog; initial seed uses active assigned services. Campaign type must be manual/re_engagement, recipient customer, and channel strategy `single` (SMS) or `telegram_then_sms`. With `telegram_then_sms`, keep `channel=sms`: Telegram is selected for customers with a reachable chat ID, otherwise SMS. A definitive Telegram destination rejection (blocked/deactivated user or chat not found) also queues SMS through the existing durable window/rate/eligibility gates. No fallback follows provider acceptance, unread status, or an uncertain transport outcome, to avoid duplicate contact. The mandatory known-visit 3–12-month/no-future-booking rule intersects any selected audience. Promotion must be active, percentage 30, and `recipient_offer_only=true`. This prevents ordinary `NEW_MASTER` code redemption and public automatic promotion selection. Each run freezes its own configuration and each recipient receives an immutable offer snapshot. Editing a later campaign never changes issued offers.

`master_name_for_message` is an administrator-editable grammatical name, required and nonempty at launch. With the default phrase `до`, Ukrainian uses genitive: `Андрія Віканова`. No unreliable automatic name inflection is performed. The same template/promotion can be reused with another master and their grammatical name.

For new offer campaigns the omitted daytime window defaults to 10:00–18:00, every day, Kyiv; omitted speed defaults to 20 recipients/minute, frequency 7 days, cap 1 per 7 days. Explicit null window is rejected; configured windows must remain within 10:00–18:00. Weekdays use Monday=0 through Sunday=6. Start inclusive/end exclusive. Rates 1–480; frequency/cap interval 1–365 days; max contacts 1–100; cap and cap interval must both be supplied or both null. Legacy metadata-only campaigns read their old settings until updated; typed delivery columns take precedence thereafter.

`max_messages_per_minute` is accepted as a legacy input alias for `sms_recipients_per_minute`; responses use the canonical field. `quiet_hours_*` is rejected with 422 instead of silently ignored: frontend must send the positive `sending_window` contract above. Unknown campaign fields are rejected. `metadata_json` remains available for historical unrelated options but is not authoritative over typed offer/delivery configuration.

## Preview, readiness, launch and lifecycle

| Method / path | Result |
| --- | --- |
| POST `/backoffice/messaging/campaigns/{id}/audience-preview?page=1&page_size=50` | Raw audience `total`, eligibility/reasons, real personalized sample text, per-sample `sms_parts`, full-audience `communication_eligible_recipients`, `telegram_eligible_recipients`, `sms_eligible_recipients` and `estimated_sms_parts` |
| GET `/backoffice/messaging/campaigns/{id}/readiness` | `ready`, per-check `code/status/detail`, runtime-verification status, balance status |
| POST `/backoffice/messaging/campaigns/{id}/runs` | 201 persisted idempotent run; body `{"idempotency_key":"admin-generated-uuid"}` and optional aware `scheduled_at`; optional positive `test_customer_id` restricts a test run to exactly that selected eligible customer |
| GET `/backoffice/messaging/campaigns/{id}/runs` | Paginated run list |
| GET `/backoffice/messaging/campaigns/{id}/runs/{run_id}` | Frozen config/segment snapshots and delivery counts |
| GET `/backoffice/messaging/campaigns/{id}/runs/{run_id}/members` | Recipient snapshots/status/errors |
| GET `/backoffice/messaging/campaigns/{id}/runs/{run_id}/queue` | Durable queue progress and estimated completion |
| POST `/backoffice/messaging/campaigns/{id}/pause` or `/resume` | Existing campaign lifecycle control |
| POST `/backoffice/messaging/campaigns/{id}/runs/{run_id}/cancel-unsent` | Cancels unsent jobs, returns cancelled count |

Run statuses: `scheduled`, `snapshotted`, `completed`, `cancelled`, `failed` (use OpenAPI/current response as authoritative). Campaign statuses remain `draft`, `active`, `paused`, `completed`, `archived`. Recipient `sent` means provider accepted, never delivered; `delivered` requires delivery confirmation. Uncertain transport outcomes remain failed/uncertain and must not be retried manually as though safely unsent.

If start is in the future and no schedule is supplied, evaluation is scheduled for start. Explicit schedules must lie inside offer validity. Readiness blocks launch when dates, master/promotion/services, template, eligible audience, provider configuration or actual local scheduler/worker activity are missing. Each scheduler/worker must have a live named task and a recent successful iteration; flags alone are insufficient. Multi-process deployments conservatively require verification in the API process receiving launch requests. Production rollout verification is still required.

Financial estimates are explicit: `estimated_cost={"status":"unavailable","amount":null}`, `balance={"status":"unsupported","amount":null}` because the existing provider adapter exposes no authoritative tariffs/balance capability. SMS parts reflect full personalization, including link length, GSM extension septets and UCS-2. These are estimates, not a charge. Preview placeholder token has the same cryptographic token length as issued links. A future balance integration must not promise funds remain available at send time.

## Public booking handoff

| Method / path | Payload / response |
| --- | --- |
| GET `/public/campaign-offers/{token}` | Safe context: `master_id`, `service_ids`, `discount_percent`, `name_uk/name_en`, `starts_at/expires_at`, `entitlement` (`available/reserved/consumed/unavailable`). Counts technical link requests only. |
| POST `/public/campaign-offers/{token}/opens` | `{"event_id":"stable-frontend-page-session-uuid"}`; 204 no content, deduplicated confirmed open |
| POST `/public/bookings/quote` | Existing quote fields plus `offer_token`; no writes, reservations, consumption or SMS |
| POST `/public/bookings` | Existing booking fields plus `offer_token`; atomically prices and attributes booking and reserves entitlement |

Preselect context master/services and show 30%. Require customer to complete the existing booking identity flow; the token does not replace it. Preserve a stable frontend `event_id` across reloads so duplicate opens do not inflate counts. Call confirmed-open only once the booking page has actually mounted and is visible; backend confirmation is an explicit frontend assertion, not proof against arbitrary malicious clients.

Example quote/create additions: `{"master_id":4,"service_id":18,"customer_phone":"<existing booking identity>","start_at":"<chosen aware timestamp>","offer_token":"<opaque-token>"}`. Retain all existing required quote/create fields and approved `expected_total_amount` behavior. Price 800 yields discount 240 and total 560. Another customer/master/service, expired offer, changed audience, unavailable catalog or reserved/consumed entitlement is rejected. Client discount/promotion IDs cannot authorize entitlement. No other automatic promotion stacks. Existing admin manual adjustment policy remains separate.

Cancellation/no_show releases an unconsumed reservation; completion permanently consumes it and snapshots completed revenue/discount. Parallel creation serializes customer/offer locks and has a unique persisted reservation constraint. Completed historical bookings are retained; deletion of any attributed booking returns 409 and directs cancellation. Repricing/revalidation preserves existing approved prices; rescheduling outside offer validity is rejected.

## Errors and analytics

Errors use standard FastAPI `detail`. Validation errors are 422. Relevant offer errors: 404 `offer_not_found`; 410 `offer_expired_or_not_started`, `offer_visit_outside_validity`; 403 `offer_identity_or_master_mismatch`; 409 `offer_reserved`, `offer_consumed`, `audience_no_longer_eligible`, `future_booking`, catalog-unavailable errors. Launch returns 409 `campaign_not_ready` with readiness diagnostics; missing/out-of-range validity may return 422, expired snapshot 410. Handle `detail` as either structured object or legacy string. Public token pattern violations are 422. OpenAPI exposes the concrete request/response models.

GET `/backoffice/messaging/campaign-offer-analytics` accepts optional `campaign_id`, `run_id`, `master_id`, `promotion_id` filters together, plus aware ISO `date_from` (inclusive) and `date_to` (exclusive). The date period selects run cohorts by `evaluated_at`, falling back to `created_at` before snapshot. All metrics are cumulative outcomes for those selected cohorts, not events occurring within the period; response `period_basis=run_cohort` makes this explicit. Reversed bounds or naive timestamps return 422. Metrics: raw audience; snapshot communication-eligible recipients; provider accepted; delivered; raw link requests; confirmed opens; distinct recipient customers opening; bookings created; current cancelled/no_show bookings; permanently completed offer visits; distinct redeemed customers; immutable total redeemed discount and observed completed revenue. Each source is aggregated separately to prevent join inflation. Provider accepted remains counted if later marked undeliverable. Raw GET requests (including scanners) do not count as confirmed opens. Confirmed events are unique by offer and event ID.

`booking_conversion_percent = bookings_created / communication_eligible_recipients × 100`; `redemption_conversion_percent = unique_customers_redeemed / communication_eligible_recipients × 100`. Zero denominator returns null. Cancel-and-rebook can produce booking conversion above 100%; redemption remains one completed visit per entitlement. Completed metrics retain first-completion snapshots even if an admin later corrects booking status/prices. Revenue is observed, never described as incremental.

## Rollout order and required configuration

1. Back up database and apply Alembic `0099_campaign_offers`; verify legacy campaigns/transactional notifications in staging.
2. Deploy backend with `CAMPAIGN_RUN_SCHEDULER_ENABLED=true`, `SMS_QUEUE_WORKER_ENABLED=true`, `SMS_PROVIDER=smsclub`, valid `SMS_CLUB_TOKEN`, registered `SMS_SENDER_NAME`, and correct HTTPS `PUBLIC_SITE_URL`. Keep existing transactional settings. Compose now passes campaign scheduler and queue enable flags explicitly; defaults do not activate campaigns.
3. Run `python -m app.utils.seed_new_master_campaign` explicitly against the intended environment. This checks master 4's verified name and creates only missing compatible promotion/template/segment/draft. It never selects dates, creates runs or sends messages. If an identity/rule conflict exists it stops without overwriting.
4. Deploy sc-fe contract and verify token route/query persistence, mounted-page confirmation, quote and existing booking creation in staging with fake/test provider.
5. Configure actual dates/service scope and grammatical name in back office; review personalized full-audience preview. Wait for successful scheduler and worker iterations, verify readiness, queue instrumentation and delivery callbacks.
6. Conduct an explicitly authorized restricted test recipient run, including 18:00 deferral/restart, pause/cancel, completion/cancellation, analytics and transactional regression. Production customer launch is a separate explicit action.

No real campaign has been launched by this change. Production readiness remains unconfirmed until the rollout above is actually observed. Frontend integration is verified against a disposable real test backend with fake messaging providers. Current cost/balance capability remains unavailable/unsupported; operational production verification remains deployment work. Configure TELEGRAM_BOT_TOKEN when selecting Telegram → SMS fallback.

## Explicit test recipient run

The existing run launch body accepts optional `test_customer_id`. This is an explicit administrator action, not an ordinary test-message call. The selected customer must satisfy the campaign audience, mandatory inactivity/no-future-booking, communication consent and contact caps. An ineligible selection returns 409 without creating a run. The ID is frozen in the run snapshot and remains enforced even for a delayed snapshot; no other recipient is included. Readiness, daytime delivery, speed, provider and idempotency gates remain in force. Reusing a launch key with a different test selection returns 409 `launch_idempotency_conditions_changed`. This is a real restricted campaign run that activates the campaign and issues a real entitlement; it consumes contact caps on acceptance and appears in run analytics. Draft save and preview never trigger it. Ordinary `send-test` remains rejected for offer campaigns.
