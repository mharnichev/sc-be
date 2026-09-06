# Durable SMS delivery and throttling

## Provider limits verified

Verified against [SMSClub's official API documentation](https://smsclub.mobi/api/) on 2026-09-06: up to 100 phone numbers sharing one message per send request; up to 9 API requests/second per account; up to 100 message IDs per status request. This implementation sends one recipient per request. Default account allowance 8 requests/second leaves headroom. API limit applies to sends, OTP, service notifications and status polling together.

## Configuration

All application processes using one SMSClub account must share the same PostgreSQL database and `SMS_CLUB_ACCOUNT_KEY` (default `primary`). The key identifies the account independently of token rotation; credentials are not stored in queue jobs. Requests made outside this application/database cannot participate in this limiter.

| Setting | Default | Purpose |
| --- | --- | --- |
| `SMS_CLUB_REQUESTS_PER_SECOND` |8| Account request spacing; allowed 0.1–8, below provider ceiling |
| `SMS_CAMPAIGN_RECIPIENTS_PER_MINUTE` |60| Default campaign SMS recipient throughput |
| `SMS_QUEUE_BATCH_SIZE` |50| Maximum work considered per worker iteration |
| `SMS_QUEUE_CONCURRENCY` |2| Maximum simultaneous provider operations across account workers |
| `SMS_QUEUE_WORKER_ENABLED` |true| Enable SMSClub durable queue worker |
| `SMS_QUEUE_POLL_SECONDS` |0.1| Idle polling interval |
| `SMS_QUEUE_MAX_ATTEMPTS` |5| Bound on explicitly retryable provider attempts |
| `SMS_QUEUE_RETRY_BASE_SECONDS` |1| Exponential retry base |
| `SMS_QUEUE_RETRY_MAX_SECONDS` |60| Exponential retry cap before provider Retry-After |
| `SMS_QUEUE_LEASE_SECONDS` |30| In-flight claim lease |
| `SMS_QUEUE_WAIT_SECONDS` |30| Synchronous OTP/service caller wait; queue survives caller timeout |
| `SMS_QUEUE_TTL_MINUTES` |1440| Default job expiry; OTP/service lifetimes can be shorter |

Batch size, concurrency and throughput are separate controls. Increasing batch size does not raise either throughput limit. PostgreSQL account locking and clock-based pacing coordinate every worker; restarts do not reset reservations or provider cooldowns. Priority order is OTP (0), service notifications (10), marketing (100), status polling (200). Dispatch already in progress cannot be preempted.

## Campaign contract

Existing `POST /api/v1/backoffice/messaging/campaigns` accepts these additional fields alongside `segment_ids` or legacy `audience`:

```json
{
  "name": "Return offer",
  "type": "manual",
  "channel": "sms",
  "purpose": "marketing",
  "segment_ids": [12],
  "sms_recipients_per_minute": 60,
  "sending_window": {"start": "09:00", "end": "20:00", "days": [0,1,2,3,4]},
  "exclude_upcoming_booking": true,
  "exclude_returned_since_snapshot": true,
  "metadata_json": {"message_body": "{{client}}, your return offer is ready."}
}
```

Sending windows use Europe/Kyiv, weekdays Monday=0 through Sunday=6. Start is inclusive and end exclusive; overnight windows belong to their opening weekday. Equal start/end is rejected; a null window allows any time. A nonexistent DST time normalizes forward, and a repeated time uses its earlier occurrence. Creating a draft sends nothing. Launching `/runs` or the customer-marketing `/start` persists recipients; it does not drain the audience in the launch request or its background task. Workers create durable SMS jobs and dispatch under both account and campaign limits.

Existing campaign pause/disable and resume/enable controls affect queued work. `POST /campaigns/{id}/runs/{run_id}/cancel-unsent` cancels only unsent work in that run; provider-accepted SMS cannot be recalled. Response is `{run_id,cancelled,status}`. The worker checks current campaign/run state and recipient eligibility again at actual dispatch, including consent, contact restrictions and selected return exclusions.

## Queue API

Every endpoint below uses existing active-admin bearer authentication and the base `/api/v1/backoffice/messaging`.

- `GET /sms-queue`: account configuration, status counts, next request slot and provider cooldown.
- `GET /sms-queue/jobs?state=queued&page=1&page_size=50`: paginated durable operation records; page size 1–100.
- `GET /sms-queue/jobs/{job_id}`: individual operational status, attempt count, next eligibility time, lease, provider ID and error reason. Message bodies, OTPs, credentials and raw provider payloads are excluded.
- `GET /campaigns/{id}/queue`: campaign dispatch progress across runs.
- `GET /campaigns/{id}/runs/{run_id}/queue`: one run's dispatch progress.
- Existing run members and message logs retain per-recipient identity and delivery results; `sms_queue_job_id` links a recipient to its durable SMS job.

Progress fields: `{total,counts,dispatching,paused,cancelled,sms_recipients_per_minute,estimated_remaining_seconds,estimated_completion_at,next_window_at,estimate_kind:"dispatch",estimate_note}`. Counts distinguish queued, provider-accepted, delivered, failed, skipped and uncertain. Dispatching is also reported separately. Cancelled unsent recipients are skipped with a cancellation reason.

**Dispatch completion is not delivery completion.** Estimates use queued work, configured throughput, current account pacing/cooldown, known priority work and sending windows. They cannot predict future priority traffic or retries. Aggregate ETA is unavailable when multiple runs are pending; inspect individual runs for their estimates. Paused/cancelled work has no active dispatch ETA. Provider acceptance and delivery reports are distinct states; accepted does not mean read or delivered.

## Retry and recovery semantics

Explicit HTTP 429 rejection retries with bounded exponential backoff and jitter. A supplied Retry-After is respected as an account-wide cooldown. Read-only status requests may safely retry transport/5xx failures. Send-time network timeouts, lost connections, malformed success responses or absent recipient acceptance IDs are uncertain, because the provider may already have accepted the SMS. Generic send 5xx responses are also treated conservatively as uncertain. HTTP 453 duplicate protection is terminal rather than retried after the protection interval.

A durable claim is committed before provider I/O. Concurrent workers cannot send the same job. Expired claims whose transport may have started become uncertain and are not made sendable again; abandoned claims that never reached transport can safely be reclaimed; safe read-only operations can be recovered for retry. Accepted outcomes and per-recipient provider IDs survive restart. Outcome projection can be retried independently of sending. A synchronous caller timing out does not erase or duplicate queued work.

This release uses individual sends. A rejection or timeout affects only that recipient; there is no assumed whole-batch acceptance. SMSClub status reconciliation continues through the same account limiter.

## Rollout

1. Apply `0069_sms_queue_throttling` after `0068_customer_segments` in the normal controlled release process.
2. Deploy all API and worker processes together with the same account key, database, rate and concurrency configuration.
3. Verify OTP, service and campaign jobs against a sandbox transport. Review queue age and configured throughput before enabling campaigns.
4. Run the durable SMS worker for restart recovery; enable the existing campaign-run scheduler for scheduled audience generation. A mixed deployment with old processes bypasses the new shared limiter and is not supported.

The migration is additive. Downgrade discards queue/throttle history, so drain or explicitly cancel queued work and retain needed audit data first. No production migration, deployment or live SMS send is performed by the implementation task.
