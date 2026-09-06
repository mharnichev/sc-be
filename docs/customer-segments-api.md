# Customer segments and campaign runs API

Implementation contract. All operations use the existing backoffice bearer authentication and base `/api/v1/backoffice`.

## Resource layout

- `/segments`: GET paginated list, POST create a reusable dynamic audience.
- `/segments/preview`: POST unsaved rules, evaluation timestamp and pagination; returns membership and explanations.
- `/segments/{id}`: GET, PATCH with expected revision (optimistic concurrency).
- `/segments/{id}/archive`: POST archives without deleting campaign history.
- `/segments/{id}/members`: GET paginated dynamic membership and explanations.
- `/messaging/campaigns`: existing CRUD; `segment_ids` references reusable segments. Creating from segments always creates a draft.
- `/messaging/campaigns?view=campaigns|notifications`: separate frontend views; omitting view preserves the existing combined list.
- `/messaging/campaigns/{id}/audience-preview`: POST paginated live recipients, exclusions and channel reachability.
- `/messaging/campaigns/{id}/runs`: POST launch or schedule using an idempotency key; GET history.
- `/messaging/campaigns/{id}/runs/{run_id}`: GET frozen configuration and delivery summary.
- `/messaging/campaigns/{id}/runs/{run_id}/members`: GET immutable membership joined with current delivery outcomes.

The schemas and examples below accompany the generated `/openapi.json` contract.

## Segment rules and history semantics

`SegmentRules` has `combine: "all" | "any"`, 1–20 `conditions` and 0–20 `exclusions`. Any matching exclusion removes a member regardless of the combine mode. Conditions are discriminated by `type`; unknown fields/types are rejected. IDs are positive and lists are bounded at 50 items. No executable expressions or SQL are accepted.

| Type | Parameters | Meaning |
| --- | --- | --- |
| `last_visit_age` | `min?`, `max?`, `unit`, `min_inclusive=false`, `max_inclusive=true` | Known most recent completed/imported visit; at least one bound |
| `completed_visit_count` | `min?`, `max?`, `period?` | Observed completed booking count, inclusive bounds; not inferred from imports |
| `upcoming_booking` | `present=true` | Pending or confirmed booking starting at or after evaluation |
| `visited_master` | `master_ids`, `mode="last"|"within_period"`, `period?` | Latest known master or one visited during a period |
| `received_service` | `service_ids`, `period` | Existing `barber_services.id` values, including multi-service booking items |
| `first_visit` | `period` | Earliest observed completed visit, unavailable when earlier imported evidence exists |
| `received_campaign` | `campaign_id`, `period?` | Provider-accepted message in this campaign |
| `marketing_contact` | `period`, `present=true` | Presence/absence of provider-accepted marketing contact across campaigns |

A `period` is either `{ "start": "2026-01-01T00:00:00+02:00", "end": "2026-04-01T00:00:00+03:00" }` or `{ "last": 3, "unit": "calendar_months" }`. Start is inclusive, end exclusive; future evidence is excluded. Relative periods end at the evaluation timestamp. `days` means fixed 24-hour intervals; `calendar_months` preserves Europe/Kyiv wall-clock time and clamps the day to the target month's last day. Ambiguous local times use the earlier occurrence; nonexistent wall times normalize through UTC. All conditions and exclusions share the returned `evaluated_at`.

Completed visits use `Booking.end_at` with `status=completed` and `end_at <= evaluated_at`. Cancelled/pending/confirmed bookings do not count as visits. `imported_last_visit_at <= evaluated_at` contributes only to the last-visit timestamp. It does not invent a count, master or service. History is `known` when completed or imported evidence exists, `no_visits` only when an explicit `imported_is_new_client` flag establishes it with no contradictory evidence, otherwise `unknown`. Missing history never matches inactivity. Counts are observed system bookings; zero-count predicates do not turn unknown or imported-only history into a known zero. A newer imported visit makes the latest master unknown.

These are evaluations of currently stored facts at a timestamp, not time-travel reconstruction of past database versions. Repeat pages with the same `evaluated_at` for consistent date boundaries; live membership may still change if source facts are edited. Campaign-run membership is frozen separately.

### Create the default return audience

`POST /segments`

```json
{
  "name": "Last visit 3–12 months ago",
  "description": "Known visit history, excluding upcoming bookings",
  "rules": {
    "combine": "all",
    "conditions": [{"type": "last_visit_age", "min": 3, "max": 12, "unit": "calendar_months"}],
    "exclusions": [{"type": "upcoming_booking", "present": true}]
  }
}
```

At `2026-09-06T12:00:00+03:00`, this accepts last visits on or after `2025-09-06T12:00:00+03:00` and strictly before `2026-06-06T12:00:00+03:00`. Three calendar months and 90 days are intentionally different.

Create returns 201 and `{id,name,description,status:"active",rules,revision:1,created_at,updated_at,archived_at:null}`. GET list accepts optional `status`, `limit` (1–200, default 50), `offset` (0–1,000,000), returning `{items,total,limit,offset}`. GET members uses the same pagination plus optional offset-aware `evaluated_at`. POST preview accepts `{rules,evaluated_at?,limit?,offset?}`.

Preview/member responses contain `{evaluated_at,timezone:"Europe/Kyiv",total,items,limit,offset}`. Each item includes `customer_id`, `name`, `phone`, `history_state`, `last_visit_at`, `completed_visit_count`, `first_completed_visit_at`, `has_upcoming_booking`, and `conditions`/`exclusions`. Each explanation contains the validated `rule`, boolean `matched`, factual `value`, and applicable resolved period/cutoff timestamps.

PATCH uses `{ "expected_revision": 1, "name": "Updated name", "rules": {...} }`; omitted fields are unchanged, description may be null. Every successful edit increments revision. Stale revisions and edits to archived segments return 409. Archive is idempotent, increments revision once, and sets `archived_at`. There is no hard-delete endpoint.

## Validation and authentication

All endpoints require an active backoffice admin bearer token. Unauthenticated and customer/master credentials receive 401. Request validation uses FastAPI's 422 `{detail:[{loc,msg,type,...}]}`; service-level validation uses `{detail:"reason"}`. Missing segment/run resources return 404; stale/archived lifecycle conflicts return 409. Invalid timezone-less timestamps, reversed bounds, unknown operators, excessive rule/list sizes, null rules and blank names return 422. Run IDs are checked against their campaign IDs.

## Draft campaign example

`POST /messaging/campaigns`

```json
{
  "name": "September return offer",
  "type": "manual",
  "purpose": "marketing",
  "channel": "telegram",
  "segment_ids": [12, 18],
  "channel_strategy": "telegram_then_sms",
  "exclude_returned_since_snapshot": true,
  "exclude_upcoming_booking": true,
  "marketing_frequency_days": 7,
  "discount_code": "RETURN",
  "metadata_json": {"message_body": "{{client}}, welcome back! Your offer code: {{discount_code}}"}
}
```

Use an existing `template_id` instead of inline body where appropriate. Selected segment IDs are combined as a union and deduplicated by customer ID. Do not provide both `segment_ids` and inline `audience`. Creating a segment campaign returns draft even if active was requested and enqueues nothing. Reusable segment campaigns are manual or re-engagement marketing campaigns. The code only references an existing offer; promotion redemption rules remain unchanged.

Campaign updates retain references to current saved segments; they do not copy mutable rules. API responses expose the explicit options, which are persisted in reserved validated metadata keys for schema compatibility. Old inline audience filters and combined campaign listing remain supported. Their historical booking/filter interpretation is deliberately unchanged; new segment audiences use the completed/imported history rules above.

`GET /messaging/campaigns?view=notifications` includes booking confirmations, appointment reminders, review requests, master schedule reminders and master booking creation/cancellation notifications. `view=campaigns` returns the remaining types. Omitting `view` preserves the combined list. Notifications retain their existing event logic and queue/provider infrastructure.

## Preview, launch, schedule and inspect

`POST /messaging/campaigns/{id}/audience-preview?page=1&page_size=50` returns `{evaluated_at,total,page,page_size,items}`. Items show membership separately from contact permission: `{customer_id,name,eligible,exclusion_reason,channel,reachability:{sms,telegram},facts}`. Facts are grouped by segment ID. The total is audience membership, not guaranteed sends. Page size is 1–100. Preview does not create a run or send.

`POST /messaging/campaigns/{id}/runs`

```json
{"idempotency_key":"september-return-2026","scheduled_at":"2026-09-10T10:00:00+03:00"}
```

Omit `scheduled_at` for immediate snapshot creation (or use the campaign's configured schedule). The client key is required, 1–128 characters, scoped to the campaign. Retrying a key returns the same run, including its original schedule; a new key creates a new run. Creation returns 201 and `{id,campaign_id,idempotency_key,status,scheduled_at,evaluated_at,segment_snapshots,campaign_snapshot,audience_count,created_at,updated_at}`. The stored key includes a campaign prefix.

Scheduled runs snapshot at the first worker claim on or after `scheduled_at`, using the actual recorded `evaluated_at`. Segment/campaign edits before that point affect the eventual snapshot. At snapshot, segment IDs/names/revisions/rules, message configuration, rendered messages, audience IDs and inclusion facts are frozen atomically. Edits after snapshot cannot alter those records. Archiving a referenced segment before a scheduled snapshot prevents the new snapshot and exposes a failure reason. Existing historical runs remain inspectable.

`GET /messaging/campaigns/{id}/runs?page=1&page_size=50` lists runs. Detail adds `delivery_counts` grouped by delivery status. `/members` returns the standard `{total,page,page_size,items}` with immutable `snapshot_facts` and live recipient status, chosen channel, attempts, sent/delivered timestamps, provider ID, `last_error`, and `send_started_at`. The legacy recipient/log inspection endpoints remain usable. Deleting a campaign with runs archives it to retain history. Customer deletion follows the existing deactivate-instead-of-delete policy for customers with history, extended to customers appearing in run snapshots.

For customer marketing types, the legacy `/start` endpoint uses a stable per-campaign launch key and returns `run_id` plus its existing campaign/enqueued/status fields. Repeated starts cannot create duplicate audiences. Use explicit new `/runs` keys for intentional additional runs. Existing inline birthday, first-visit follow-up and loyalty/VIP campaigns also support runs. Legacy service-event/master `/start` calls retain their existing enqueue path and response.

## Delivery rules and observability

SMSClub launch paths enqueue durable work. The shared account limiter, separate recipients-per-minute control, sending windows, cancellation and queue progress API are specified in [SMS queue API](sms-queue-api.md). SMS queue outcomes additionally expose `queued`, `accepted` and `uncertain` explicitly while retaining legacy recipient statuses.

`single` uses the configured channel. `telegram_then_sms` chooses Telegram when a chat destination is available and otherwise SMS; `sms_then_telegram` reverses that availability preference. Exactly one channel is selected for a recipient. A provider-accepted or unread message never triggers another channel. Provider timeouts/errors after dispatch are treated as uncertain acceptance and are not automatically resent or switched to another channel.

The worker rechecks customer activity, current communication preference/consent, contact restrictions, optional upcoming booking/returned exclusions and the marketing frequency cap immediately before reserving delivery. Returned means an authoritative completed/imported last visit strictly after snapshot evaluation. Consent defaults retain existing behavior. Marketing contact history spans campaigns; transactional notifications and review-request frequency rules remain separate. For legacy rows, marketing requires both marketing purpose and a marketing campaign type (manual, re-engagement, birthday greeting, first-visit follow-up or loyalty/VIP), so legacy service-event campaigns with the old default purpose are not accidentally capped.

Recipient uniqueness `(run_id, customer_id)` and the idempotency key prevent overlapping audiences and retries from creating duplicates. A durable `send_started_at` reservation is committed before contacting providers; customer locking serializes marketing reservations across campaigns. This deliberately favors avoiding duplicate sends over retrying a message whose acceptance is unknown. Provider adapters currently lack provider-side idempotency keys, so exactly-once external delivery cannot be promised. For non-queued legacy delivery, a pending reservation older than 15 minutes is reconciled to `failed` with `delivery_uncertain: worker_interrupted`, without a resend.

Outcomes include pending, sent, delivered, failed and skipped; `last_error` records reasons such as consent/contact restriction, upcoming booking, returned since snapshot, marketing frequency cap, unavailable channel/provider or uncertain delivery. `sent` means provider acceptance; `delivered` means a provider delivery report. Neither means read. Segment `received_campaign` and `marketing_contact` rules use sent/delivered recipient records with `sent_at`, never read tracking. Frequency protection also counts recent durable in-flight/uncertain reservations to prevent cross-campaign duplicate contact.

## Rollout and operations

1. Back up and apply `0068_customer_segments` after `0067_merge_shampoo_categories` using the normal release process. It adds segment/run tables, nullable recipient columns and audience/history indexes; there is no customer backfill or promotion data mutation. Plan index build time against actual table size.
2. Apply `0069_sms_queue_throttling` next and follow the coordinated SMS worker rollout in [SMS queue API](sms-queue-api.md). Deploy backend code with `CAMPAIGN_RUN_SCHEDULER_ENABLED=false` (default), then verify OpenAPI, authenticated CRUD, preview and fake-provider delivery on staging.
3. Ship frontend Segments plus separate Campaigns/Notifications views. Existing clients can keep their inline filters and combined list.
4. Enable `CAMPAIGN_RUN_SCHEDULER_ENABLED=true` when intended. `CAMPAIGN_RUN_SCHEDULER_INTERVAL_SECONDS` defaults to 30 (5–3600). It processes due runs and only their recipients. Alternatively, the authenticated existing `POST /messaging/jobs/process-pending` explicitly processes due runs before draining its existing queue.

Downgrade removes new run/segment data and recipient snapshot columns; do not downgrade if that history must be retained. Only disposable local test migrations were performed by this implementation task; no production migration, deployment, scheduler enablement or real delivery was performed.
