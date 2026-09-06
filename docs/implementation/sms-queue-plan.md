# Durable SMS queue implementation plan

1. Verify SMSClub limits and inspect every SMS entry point. DONE: official API documents 100 send recipients, 9 account requests/sec, 100 status IDs.
2. Shared durable SMSClub operation queue, PostgreSQL account limiter, priority claims and typed retry outcomes (segments agent).
3. Campaign enqueue-only SMS, recipient throughput, sending windows, controls and eligibility/outcome projection (campaigns agent).
4. Migration/config/API/scheduler and direct notification idempotency integration (primary agent).
5. Independent PostgreSQL/fake-HTTP tests including 1,000 recipient campaign, repeated concurrent workers, recovery and review (verification agent).
6. Resolve review/performance findings, full regression suite, graphify update and handoff (primary agent).

## Decisions

- Individual sends preserve per-recipient accounting; no batch partial-acceptance ambiguity introduced.
- Account default 8 requests/sec enforced below documented 9; all SMSClub calls, including OTP/service/status, share PostgreSQL account key.
- Campaign recipients/minute, worker batch size and global provider concurrency are independent settings.
- OTP priority 0, service 10, marketing 100, status polling 200; an already-dispatched HTTP request cannot be preempted.
- HTTP 429 is explicit retryable rejection; Retry-After suspends the account. Generic send 5xx/network timeout/malformed acceptance are uncertain rather than blindly retried. Read-only status failures may safely retry. 453 duplicate protection is terminal, not delayed re-send.
- User authorization excludes real SMS and production/staging deployments/migrations. Use disposable local PostgreSQL and fake low-level provider only.

## Completion

All six implementation steps are complete. Independent findings were resolved; final regression: 730 passed, no skips or failures. Migration round trips, the 1,000-recipient fake-provider smoke, rate/priority checks, lifecycle races and restart recovery are documented in [verification](sms-queue-verification.md). The API and rollout contract is in [SMS queue API](../sms-queue-api.md). Graphify outputs were refreshed. No real customer messages or production changes were made.
