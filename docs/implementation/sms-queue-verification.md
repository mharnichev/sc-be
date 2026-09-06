# SMS queue verification

## Environment and scope

SMSClub limits were checked against the official API documentation on 2026-09-06: 100 recipients per shared-body send, 9 API requests per second per account, and 100 IDs per status query. Production uses individual send jobs and defaults to 8 requests per second for headroom.

Database tests use disposable PostgreSQL 16 on loopback port 55439 (`soulcuts-segments-test-20260906`), with a fresh temporary schema per test. Integration fixtures require an explicit local test database URL. Provider tests replace the low-level HTTP transport with a recording fake, so the real SMS service, queue, PostgreSQL coordination and recipient projections run without sending a message to SMSClub. No production database, deployment or customer messaging was used.

## Coverage

- A real campaign snapshot of 1,000 synthetic customers is enqueued without provider traffic, then processed by three independent workers. Personalized sends produce 981 accepted outcomes with unique provider IDs, 10 explicit failures and 9 uncertain outcomes, with no duplicate recipient attempts. This large smoke test advances an injected clock; it is not a wall-clock load benchmark.
- Separate real-clock tests check actual fake-HTTP start times across independent workers against the configured account rate. OTP, two campaigns, service sends and status operations share admission and priority. Delayed eligibility callbacks cannot reserve future slots and then bunch requests.
- Campaign recipients-per-minute is tested separately from HTTP requests-per-second. Sending windows use Europe/Kyiv, including overnight windows and weekday boundaries. Pause/resume, changed consent and exclusions, cancellation after eligibility but before transport, and cross-campaign marketing deduplication are covered.
- Queue idempotency, transaction rollback, new-worker recovery, expired claims, provider 429/Retry-After, exhausted retry cooldown, explicit failures, ambiguous timeouts and safe read-only retries are covered. Callback failures keep retry jobs blocked until business state has been reconciled.
- Delivery receipts retain provider acceptance timestamps and IDs, advance each recipient independently, and do not downgrade delivered outcomes.
- Concurrent secure-link notification enqueue attempts commit one token and one job; retries preserve the token and cause one fake-provider send.
- Operational API tests verify admin authentication and response-schema exclusion of message bodies, OTPs, credentials and raw provider responses. Campaign progress calculations are separately exercised against PostgreSQL; the new queue read endpoints do not have a separate authenticated HTTP/database smoke test.
- The actual 0069 migration is upgraded, downgraded and upgraded again on disposable PostgreSQL while retaining the preceding segment/run/recipient history.

## Review fixes

Independent review and tests found and resolved account admission drift after slow eligibility callbacks; retry reservation projection races; expired pre-transport claim recovery; final-attempt rate-limit cooldown; cancellation and pause races before transport; stale delivery receipts; ETA query ambiguity; and waitlist context validation. Queue outcome callbacks are serialized per job without holding a queue row lock while acquiring business locks.

The first full regression run found two failures (ETA query ambiguity and a misplaced receipt guard). Both were corrected and are included in final regression verification.

## Reproduce

```sh
SEGMENTS_TEST_DATABASE_URL='postgresql+asyncpg://segments_test:segments_test@127.0.0.1:55439/segments_test' python3 -m pytest -q
```

**Final result: 730 passed, no failures or skips, 13 existing deprecation warnings, 147.88 seconds.** Compilation and git diff whitespace checks passed. The final graphify AST refresh completed with 3,868 nodes, 13,976 edges and 213 communities. Its local cache permission issue was resolved using writable temporary cache directories. The disposable PostgreSQL container was stopped after verification. These checks establish local PostgreSQL and fake-provider behavior, not live provider delivery or production throughput. External applications sharing the SMSClub account must use the same database/account gate to participate in its rate limit. Sending windows, higher-priority work and retry delays affect dispatch estimates; dispatch completion never implies delivery completion.
