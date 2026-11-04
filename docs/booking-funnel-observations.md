# Availability diagnostics v1

Backend-first rollout: upgrade to 0145_booking_funnel_observations, deploy API,
then clients. Old /booking-funnel/events clients, meanings, calculation_version=3,
cohort calculations and success backfills remain intact. Migration downgrade
removes only the new observations; stop new API before downgrade.

POST /api/v1/public/booking-funnel/observations accepts exactly:
- event_id: 8–128 ASCII letters/digits or ._:-.
- anonymous_session_id: SAME existing attempt ID, same syntax, 16–128.
- event_type: time_step_viewed, availability_requested, availability_result,
  availability_failed, availability_cancelled.
- master_id: requested/source master positive integer.
- service_ids: complete selected set, 1–10 positive integer IDs, sorted/deduped.
- target_date: ISO Kyiv calendar date; duration_minutes: integer1–720.
- trigger: initial, date_change, refresh.
- availability_request_id: ID8–128 required for availability events only.
- slot_count: integer0–5000 required for result only, including zero.
- error_code: required for failed only: timeout, network, http_4xx, http_5xx,
  invalid_response.
- cancel_reason: required for cancelled only: superseded, navigation.

Unknown keys and invalid field combinations return422. No client timestamp,
arbitrary metadata, raw errors, URLs or customer data. IDs are HMAC hashed;
received time is server time.202 response: {event_id,status:recorded|duplicate}.
Retry immutable content with SAME event/request IDs. Event-ID uniqueness and
attempt/request/kind uniqueness prevent inflation. Terminal result/failure/
cancellation share one terminal key. Conflicting identity reuse returns409.
Terminal without requested event is accepted and reported as missing telemetry.
A new reload needs a new request ID. Keep captured attempt ID in bounded retry
queues after attempt reset/TTL; delivery must never block booking.
Historical no_slot remains explicit-date only with existing authoritative checks.
Default-date empty belongs only to availability_result with trigger initial.

Existing aggregate adds nullable observation_diagnostics: scope event_period,
event_counts by kind, failure_counts by bounded error_code (including timeout),
initial_empty_results, terminals_without_request, explanatory
text. Half-open [start,end) server receipt period; all-history request matching
avoids false missing-request markers across midnight. These are client reports,
not authoritative availability or evidence of causation.

Existing aggregate adds selection_diagnostics: scope booking_start_cohort,
largest_absolute_loss and largest_percentage_loss (existing drop-off shape),
small_sample_transitions, conservative observation_incomplete=true, explanatory
text. Existing same-attempt conversion denominators remain unchanged. No ranked
loss for unavailable/empty transitions. Selection milestones are not page views;
drop-off alone does not establish a broken transition. Later outcomes/lost
analytics make observations incomplete.

# Calendar semantics

resolve_booking_master follows redirects to active physical resource, rejects
cycles. Public availability uses confirmed bookings, active unexpired waitlist
holds, windows, blocks, duration, horizon and past-time rules. Monthly admin
planning statistics may keep pending/confirmed/completed/no_show and exclude
cancelled, but must label this and not claim live bookable capacity. Past time
and holds are not implicitly deducted by monthly statistics. Union intervals per
canonical physical resource, Kyiv-midnight half-open ranges, clip per day/month,
no double-counting aliases. No additional capacity endpoint is needed.

# Compatibility and investigation notes

The clean source base is cf2b6a33412af79f847bfb1e997a004d2dd2e26f (remote main,
including the already merged volume filter). Dirty-root application code is not
part of this change. New observations require no calendar/booking writes.
The deployment readiness gate must accept exact head
0145_booking_funnel_observations after upgrade; it previously expected0144.

Contact-path inspection found required name/phone/email schema validation,
canonical phone identity handling, locked redirect resolution, complete service
validation, working-hours and availability checks, financial quote consistency,
and locked slot conflict checks before insert. The existing observed drop-off
does not establish a defect in those checks; no validation was removed or booking
algorithm changed. Tests cover their retained behavior. Analytics failure is
already isolated from durable booking creation by the existing savepoint.

Ingress protection: a privacy-safe per-client process-local budget runs BEFORE
attempt-level limiting. Rotating attempts cannot bypass that client budget or
allocate unbounded session-limiter keys from one peer. Both consume retry traffic
and return429/Retry-After when exhausted. Existing trusted proxy handling determines
request.client; forwarded headers are not parsed by this endpoint. As with the
existing limiter, multi-worker/global limits require deployment ingress controls;
this process-local defense does not claim a shared cross-instance quota.
