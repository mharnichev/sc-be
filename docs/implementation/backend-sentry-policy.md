# Backend technical incident policy

Local addon to the existing production Sentry integration (deployed source
`d29d654e89fb2410eeae97230b1517a520435152`, SDK `sentry-sdk[fastapi]==2.70.0`).
The local checkout lacked that integration; its original app/core/sentry.py is
preserved here with a small manual-incident hook. There is only one SDK init,
owned by init_sentry. No production changes are performed by this addon.

The deployed backend already has `SENTRY_DSN`; do not replace it or create a new client/project. Missing/empty DSN disables reporting.
Use `SENTRY_INCIDENT_STORE` for a private SQLite file on persistent local storage
shared by all backend workers on the host (default `data/private/sentry-incidents.sqlite3`).
Do not put it under public uploads. On multiple hosts these budgets are per host;
a shared central incident limiter is required before scaling this policy across hosts.
Production deployment must mount this private state persistently; container-local
state alone does not retain suppression across replacement/release.

Existing framework error capture, scrubbers, request correlation, cron monitoring,
release/environment and trace sampling remain owned by app.core.sentry. Existing logger errors and breadcrumbs remain for background jobs. A previously
manually reported financial exception is dropped by the framework/logger event
hook, so it is not sent twice. The strict manual financial event path includes no
request bodies, user/contact details, exception messages, stack locals, provider
payloads, payment URLs, or tokens. Only fixed stage/code tags and positive internal
integer order/attempt IDs survive the final SDK allowlist. Sentry-generated event ID
and known environment are retained. Provider is mono only at actual bank stages;
no payment method is guessed when the failing request has not established one.

One reporting module owns captures. Handled validation/access/conflict, cancelled
payments, ordinary bank failure/expired/reversed, and missing invoices are silent.
Unhandled request/order creation errors are reported with a fixed stage/code.
Swallowed invoice creation uncertainty is reported immediately; retries remain
fenced by existing immutable invoice dispatch logic. Provider verification failures
and poll exceptions require three observations with less than 15 minutes between
failures; 15 minutes of inactivity resets the counter. This is an inactivity-based
threshold, with no success reset, not proof of three consecutive failures. The same
propagated exception is not captured again by HTTP or polling boundaries.

After validated bank success, persistence failures (including integrity/DBAPI
branches) report `paid_save/paid_unconfirmed` immediately. This does not itself
complete the order or import a fiscal receipt. Receipt provider failures are
separate from payment confirmation. No additional poll candidates or financial
state transitions are introduced.

Durable admission is atomic across workers: one event per step/code/order/attempt
per 15 minutes, at most 20 events/hour and 30/day per shared store. Entries are
bounded and store only hashed incident keys/counts/timestamps. Admission is reserved
before SDK capture, so delivery failure may lose one event rather than retry-spam.
Store errors fail closed and never break checkout. This is intentionally a volume
cap, not guaranteed delivery or a replacement for local operational logs.

There is currently no public payment webhook endpoint: mono is verified by GET
reconciliation. This change does not add a webhook or trust callback payloads.
Any future webhook must verify authenticity first and use this bounded reporter;
invalid signatures must not produce an event on every incoming request.
Completed/cancelled orders are still outside the existing poll selection; a
separate historical bank-to-order audit is outside this instrumentation change.

Offline verification uses a real SDK with an in-memory transport, synthetic DSN,
blocked network sockets, and existing mono MockTransport contract tests. It never
creates real invoices, payments, receipts, shifts, shipments, or notifications.
