# Closed Shop Pilot Release Candidate

Destination: `/private/tmp/sc-be-shop-pilot-20261019`.
Base: `c9ab4b3f4c6513b2c0574383ab832ed0da1ee1d3` (`origin/main`).
Source: `/Users/markgarnicev/sc/sc-be`, read-only throughout assembly.

## Scope And Provenance

Local Shop app, additive migrations, tests, release container/startup configuration,
and synthetic restore fixtures were mechanically selected from the source.
Existing main-only files were retained. No IDE state, graphs, uploads/imports,
private output, secrets, or source working-tree metadata were imported.

The frozen 188-file input set and per-file SHA-256 hashes are recorded outside
the candidate in `/private/tmp/sc-be-shop-pilot-evidence-20261019/input-manifest.json`.
Its SHA-256 is `12fa4876e527a46939dbae64c3045f765c35b64eb0cb0e7adc3b4e998882517c`.
Frozen bytes are in the sibling `frozen-input` directory. Final candidate paths,
hashes, source-drift and migration-preservation results are in `candidate-manifest.json`
and `preservation.json` in that evidence directory.

Manual changes are limited to pilot configuration, the pre-order checkout gate,
Compose/example wiring, focused pilot tests, recursive bytecode image exclusions,
sanitized scheduler labels in the existing authenticated read-only operations
watch, legacy image/migration test contract adaptations, a portable loopback port
assertion in the worker test fixture, and this release note. Worker production code and
migration/startup owner functions are retained from the frozen candidate.

## Checkout Contract

Defaults: public false, pilot false, pilot IDs empty. An empty/disabled pilot is
closed before ingress/auth/database dependencies. Malformed ID configuration
fails startup; a malformed runtime replacement fails closed.

With pilot enabled and a nonempty valid ID list, shared source admission commits
before authentication obtains a connection. Production/staging enforce this
even when `SHARED_INGRESS_LIMITS_ENABLED=false`. Denied auth spends the bounded
source budget. Unavailable admission storage fails closed without order work.
This ordering supports a pool with one connection.

The existing backoffice operations watch includes typed `scheduler_names` from
the serving process. Only known lifespan scheduler labels are returned, including
after worker failure; unknown values, exception text and arbitrary state are
excluded. Its existing current-operator permissions and master restrictions remain
unchanged. This is process-local evidence, not a claim about every replica.

Before `OrderService.create_order`, pilot access requires the existing signed
customer authentication/session contract, an explicitly allowlisted customer ID,
`is_active=true`, and non-null `phone_verified_at`. Guests, nonmembers, inactive
and unverified accounts are closed; invalid supplied authentication returns 401.
Headers, query parameters, recipient phones and payload customer IDs cannot grant
access. Existing public-true behavior remains available only outside pilot mode;
startup rejects public-true plus pilot-true configuration.

## Deployment Inputs (Not Applied)

The primary owner manages the production environment. The approved pilot identity
is customer ID `10` only. No phone number is embedded in the allowlist contract.

```dotenv
APP_ENV=production
DEBUG=false
SHOP_CHECKOUT_ENABLED=false
SHOP_CHECKOUT_PILOT_ENABLED=true
SHOP_CHECKOUT_PILOT_CUSTOMER_IDS=10
MIGRATIONS_MODE=check
```

The currently unverified production record remains ineligible until the user
completes ordinary OTP verification. Do not create another record, alter its
verification state, issue a token manually, or send OTP/SMS as a release test.
For fully closed operation use pilot false and IDs empty; do not enable the
public flag. Repository defaults remain fully closed.

Keep Shop payment/fiscal/return live transports and provider sandbox network
disabled. Keep mock/simulation switches disabled in production. Set the real
stable secret and exact HTTPS CORS origins through the existing secure mechanism.
Set `TRUSTED_PROXY_IPS` only for the verified proxy boundary; never wildcard.

One designated migration owner applies the additive `0110` to `0129` chain before
API replicas start. Replicas use `MIGRATIONS_MODE=check` and verify exact head;
they do not opportunistically migrate. No existing `0001` through `0128` migration
was manually edited. Revision `0129` only adds the ingress table and index.

Preserve existing barber scheduler flags and the designated background owner.
Do not set `BACKGROUND_WORKERS_ENABLED=false` on the sole barber owner. API-only
replicas may disable background workers only with a separately verified owner.
Ownership requires session-affine PostgreSQL, not transaction pooling. Standby
does not promote automatically; failure recovery requires operator reconciliation.

## Acceptance Limits

Local tests use synthetic data and mocked providers in newly created loopback
PostgreSQL test databases. No production application lifespan, real purchase,
payment, fiscal action, shipment, notification or provider request is authorized
by test success. No commit, push or deployment is performed by this task.

Public opening remains NO-GO. Unresolved production gates include verified pilot
authentication, backup/recovery and DB-role checks, migration/worker ownership and
session affinity, proxy anti-spoofing, catalog/stock/price reconciliation, existing
queues, real OTP delivery acceptance, operational alerts/on-call, retention/SLA,
merchant terms and separately authorized live R1-R4 acceptance. A checkout pilot
allowlist is not provider or financial-flow acceptance.

The source readiness documents remain authoritative context:
`docs/implementation/shop-background-operations-readiness.md` and
`docs/implementation/shop-production-readiness-after-b1-b3.md` in the source tree.
Final test commands, exit statuses and logs are delivered with the evidence bundle.
