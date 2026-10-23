# Automatic service reminder for one unfinished shop checkout

This implementation sends at most one service reminder for a concrete unfinished checkout after a deliberate email checkpoint. There is no per-user opt-in, checkbox, reminder-request button or `consent` API field. It never enrolls browsing history, historical carts, newsletters or marketing audiences. Features, live transport and scheduling remain disabled by default; no real email or production activation was performed in this task.

## Deliberate checkpoint and API contract

The frontend emits a checkpoint only after a trusted email blur or Enter event with a valid address, stable cart and current account/guest scope, debounced approximately800ms. It captures an immutable email/items/locale/scope snapshot. Input keystrokes alone do not persist contact information or enroll a reminder. Cart/email changes invalidate the previously saved snapshot; a subsequent deliberate email checkpoint can update that same active checkout.

- `GET /api/v1/public/checkout-reminders/capabilities` returns `{enabled,dry_run}` for the actual optionally authenticated customer, with no-store/no-referrer headers. When global checkout is closed, only an active, phone-verified authenticated customer in the enabled checkout pilot allowlist can enroll or recover. Guests, nonallowlisted users and email identity cannot bypass the gate. Invalid supplied authentication returns 401.
- Global delivery readiness permits an enabled, nonempty pilot allowlist without opening global checkout. Dispatch revalidates each persisted row's actual owner both before reservation and at the final dispatch fence; guest and no-longer-eligible owner rows are suppressed. Cancellation remains available when disabled.
- `POST /api/v1/public/checkout-reminders/checkpoint` accepts only `{email,locale:'uk'|'en',items:[{product_id,quantity}]}` with the browser's random `X-Checkout-Key`. The optional positive `X-Checkout-Revision` header fences frontend events; current clients send it. `checkpoint_at` and `checkpoint_version` record technical facts, never permission or worker eligibility.
- `POST /api/v1/public/checkout-reminders/cancel` with the same key and the next revision invalidates the pending snapshot and remains available while the feature is disabled. A versioned, never-attempted cancellation may be rearmed by a strictly newer deliberate checkpoint before the original expiry. Stale checkpoint/cancel events are idempotent no-ops. Legacy cancellation without a revision remains terminal. Completed, sent, dry-run, attempting, uncertain, suppressed and expired checkouts never rearm.
- Repeating the same revision does no write and does not extend either deadline. A newer deliberate event with identical email/items/locale advances only event metadata; it does not reset the reminder timer. Original checkout expiry is never extended.
- `POST /api/v1/public/checkout-reminders/recover` accepts `{token}` in the body. The email links to `/checkout/recover#token=...`, keeping the token out of the navigation request URL. GET never consumes recovery. The response `{items,locale,changes,checkout_key}` previews the current canonical catalog and gives this checkout's derived completion/cancellation capability. It does not authenticate, create an order, overwrite a cart or grant account access.
- Existing `POST /api/v1/public/orders` accepts optional `X-Checkout-Key`; `OrderService.create_order` completes the checkout in its order transaction, including exact historical retries. A rollback preserves the pending reminder. Completion always wins over later checkpoint revisions.
- A guest checkout accepted again with an actual authenticated customer is bound to that account under lock. Subsequent recovery requires that same account; email matching alone never associates an account. Authenticated server-cart changes are fenced at dispatch. Guest cart invalidation is sent by the frontend, including when the checkout UI is unmounted.

Canonical products retain the catalog name; the backend currently has one `name` field without a product translation contract. Surrounding email copy and variant volume units use the checkout UK/EN locale. Catalog/product files and real product data were not changed by this work.

## Delivery, suppression and recovery

Default delay is7,200seconds after a meaningful deliberate checkpoint change, with an absolute72-hour lifetime. At most one accepted reminder per checkout is permitted. Only proven rejection before acceptance can retry, bounded to three attempts with backoff. A crash leaving `attempting`, or an ambiguous provider result leaving `uncertain`, never automatically retries. Dry-run is terminal and never invokes SMTP.

The worker claims rows with `FOR UPDATE SKIP LOCKED`, validates rendering/SMTP configuration and commits a durable `attempting` record before transport. A final row lock rechecks cancellation/completion, expiry, suppression and cart contents. Authenticated cart checks share PostgreSQL `shop-cart:{customer_id}` transaction locks with cart add/delete and order creation. A completion/removal winning that fence prevents dispatch; a dispatch winning it precedes the mutation. A message already accepted by SMTP cannot be recalled.

Existing do-not-contact, blacklist, inactive-account and transactional opt-out preferences suppress matching account and guest-email checkouts. Existing `transactional_consent` is used only as suppression when opted out, never as a required opted-in enrollment flag. Suppression is rechecked before sending and recovery. Email matching for suppression never exposes account existence or establishes ownership.

Shared PostgreSQL admission limits requests by ASGI source and allows one newly admitted reminder destination per normalized email per24hours across devices/sources. Replays and same-address updates to the same checkout reuse that admission. Rearmable cancellation retains only the email HMAC for this purpose, not the raw address/items/token. Budget keys contain HMAC identities, survive rollback and fail closed when shared admission is required. Bodies, items, quantities, keys and revisions are bounded.

Recovery tokens contain32random bytes and are persisted only as purpose-separated HMAC digests. Restoring requires explicit cart confirmation in the frontend. The server revalidates current prices, stock, visibility and the same product/variant; change codes report unavailable or changed goods rather than silently replacing them. Order creation remains authoritative for final price/stock checks.

## Retention and maintenance

A sent/uncertain checkout retains its address only until its original recovery expiry so later guest opt-outs/blacklists can revoke recovery. Cancellation erases raw address, items and recovery tokens immediately; the address HMAC is retained only for a rearmable unattempted checkpoint until expiry. Other terminal cancellation/completion/suppression erases it. Expiry clears contact, snapshot, recovery capability and checkpoint metadata; tombstones disappear seven days after expiry. Reminder admission identities expire after their one-day/source-window budget. Logs contain state/error codes only; SQL parameters are hidden even with existing SQL debug echo.

`process_due` runs retention before checking the delivery feature flag. An explicitly enabled scheduler also cleans up while enrollment is disabled. When background workers or this scheduler are disabled, use the retention-only maintenance command on the operational cleanup cadence:

```sh
python -m scripts.purge_checkout_reminders --purge
```

It never sends email. Without `--purge`, it prints usage without a database connection.

## Migrations and disabled configuration

`0142_checkout_reminders` creates reminder persistence and extends shared ingress actions without historical enrollment. Additive `0143_auto_checkout_checkpoint` renames the old development columns to `checkpoint_at`/`checkpoint_version` and adds bounded `checkpoint_revision=0`, preserving existing row data and deadlines. New installations apply both migrations. The model uses the new fields directly; worker eligibility has no consent/checkpoint-version gate. Migration tests cover actual PostgreSQL upgrades, existing-row preservation, constraints, shared budgets and downgrades.

- `CHECKOUT_REMINDER_ENABLED=false`
- `CHECKOUT_REMINDER_SCHEDULER_ENABLED=false`
- `CHECKOUT_REMINDER_LIVE_ENABLED=false`
- `CHECKOUT_REMINDER_LAUNCH_APPROVED=false`: a global operator release gate, never a per-user preference or opt-in
- `CHECKOUT_REMINDER_DRY_RUN=true`
- `CHECKOUT_REMINDER_DELAY_SECONDS=7200`
- `CHECKOUT_REMINDER_TTL_HOURS=72`
- `CHECKOUT_REMINDER_RETENTION_DAYS=7`
- `CHECKOUT_REMINDER_POLL_SECONDS=60`
- `CHECKOUT_REMINDER_SOURCE_LIMIT=20` per source-limit window
- `CHECKOUT_REMINDER_SHOP_URL`: configured public HTTPS storefront URL
- `CHECKOUT_REMINDER_LOGO_URL`: public Soul Cuts email logo URL
- `CHECKOUT_REMINDER_SUPPORT_EMAIL`: configured support mailbox
- `CHECKOUT_REMINDER_ASSET_ORIGINS=[]`: additional allowed HTTPS asset origins
- `CHECKOUT_REMINDER_MEDIA_ORIGIN`: optional HTTPS origin for API-owned `/media/...` images, falling back to existing `PUBLIC_API_BASE_URL` when unset. This never changes global API or Telegram URLs. Protocol-relative, credential-bearing, query/fragment and arbitrary relative paths fail closed.

The previous development-only `CHECKOUT_REMINDER_EMAIL_OWNERSHIP_APPROVED` setting has been replaced by `CHECKOUT_REMINDER_LAUNCH_APPROVED`; it is not a customer eligibility requirement.

## SMTP and future activation

`CheckoutReminderSMTPProvider` is deployable code using existing SMTP credentials, verified TLS, one bounded transport attempt and a stable Message-ID. The worker and adapter require the live flag, global launch approval and existing `EMAIL_NOTIFICATIONS_ENABLED`; the worker also requires configured SMTP host/from and brand URLs/support. Live mode requires `CHECKOUT_REMINDER_DRY_RUN=false`; scheduling separately requires its scheduler flag. Nothing was activated by this task, and real SMTP was not called in tests.

Explicit pre-acceptance4xx rejection permits bounded retry, explicit5xx rejection terminates, and network uncertainty during DATA quarantines the attempt. A successful DATA250 remains accepted even if QUIT fails. SMTP cannot provide end-to-end exactly-once delivery: uncertain attempts require manual provider reconciliation and must not be blindly reset. No public reconciliation/reset endpoint is exposed.
