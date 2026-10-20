# Shop online mono → Checkbox

Local preparation only. No production configuration changes, bank invoices,
payments, fiscal commands or notifications were executed during implementation.
Provider tests use injected HTTP mocks and disposable PostgreSQL databases.

## Flow

1. Checkout offers `onlineCard` only when the local capabilities endpoint permits it.
2. Existing checkout creates the server-priced Shop order and reserves goods.
3. The buyer explicitly creates a payment link. The backend records an immutable
   one-shot dispatch request and canonical Shop payment intent before bank I/O.
4. mono hosts the payment page. A redirect, browser message or query parameter
   never establishes payment success.
5. The bounded background reconciler verifies the exact invoice, merchant,
   reference, currency and order total, then records the existing canonical
   Shop payment/evidence/confirmation once.
6. The same reconciler imports the receipt already issued through mono → Checkbox.
   Internal Checkbox issuance is forbidden for this fiscal strategy.
7. Existing Shop handover/dispatch operations decrement stock once, after the
   canonical payment and accepted external receipt authorize fulfillment.
   Payment/receipt polling does not physically hand over or complete an order.

## Buyer access

- Authenticated buyers can access only their own orders.
- Guests receive a dedicated signed `guest_order_payment` capability in the
  checkout response. The client keeps it in session storage, never in a URL.
- Tracking/document tokens do not authorize payment access. Expired/revoked guest
  access fails closed; authentication errors do not silently become guest access.
- `/payment/{orderId}` restores status in the same browser. A guest who loses
  the capability needs support; public knowledge of an order number grants nothing.
- GET status reads local facts only. It never creates invoices or calls providers.

## One-shot creation and recovery

An unknown create result owns the order permanently until explicit reconciliation.
A new idempotency key, another tab, a retry, or a process restart cannot authorize
another invoice POST. Staff recovery inspects an existing invoice using provider
GET requests and checks its pinned identity, exact total and creation window.
Expired/failure responses do not establish the absence of a late payment and do
not authorize automatic recreation or reserve release.

## Configuration, default closed

Keep production settings unchanged until a separately authorized release. The
new switches default to false; no merchant secrets are stored in source.

- `SHOP_ONLINE_PAYMENT_ENABLED`
- `SHOP_ONLINE_PAYMENT_INVOICE_CREATE_ENABLED`
- `SHOP_ONLINE_PAYMENT_LINK_CONFIRMED`
- `SHOP_ONLINE_PAYMENT_OPERATOR_ID`: an existing active Shop operator with current
  sales permissions, not a linked barber. This records execution provenance;
  buyer/guest initiation is separately recorded in the dispatch request.
- `SHOP_ONLINE_PAYMENT_FISCAL_POLICY`:
  `{"id":"soulcuts-shop-tax8-v1","tax_codes":[8],"unit":"шт."}`
- Existing Shop settings remain explicit: `SHOP_PAYMENTS_ENABLED`,
  `SHOP_ORDER_OPERATIONS_ENABLED`, `SHOP_MONO_CHECKBOX_ENABLED`,
  `SHOP_MONOBANK_LIVE_TRANSPORT_ENABLED`, `SHOP_MONOBANK_PROFILE`,
  `SHOP_MONOBANK_TOKEN`, `MONO_CHECKBOX_AUTO_ENABLED`.
- Pin the intended existing merchant and token fingerprint in the Shop profile,
  with `external_fiscalization_disabled:false`. Master configuration does not
  implicitly enable Shop. Never enable a second issuer for the same order.
  With explicit online routing, a separately configured internal fiscal policy
  may continue serving COD orders; persisted issuer facts always retain ownership.
- Existing checkout/pilot gates remain independently enforced.

## Reports and remaining boundaries

Payment, invoice, external receipt and fulfillment remain linked in existing
Shop financial ledgers and order projections. This is operational accounting
provenance, not a new fiscal report issuer.

mono fiscal-checks proves receipt association and provides a receipt link/PDF.
It does not provide structured receipt tax totals or X/Z report contents.
`structured_totals_verified=false` and `reports_imported=false` remain explicit.
Online-card orders select the external issuer per order. COD keeps its separately
configured existing fiscal policy; an external payment/receipt and an internal
fiscal issuer cannot share an order.

This work does not claim automatic X/Z import or reconciliation. Existing return
and cancellation policies continue to apply; this flow does not send refunds or
fiscal return commands. COD remains a separate settlement workflow.
