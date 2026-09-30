# Shop customer account API

These contracts are for `sc-fe/apps/shop`. Routes are relative to `/api/v1/public`.
Customer routes require `Authorization: Bearer <customer access_token>` returned by
`POST /customers/auth/verify-otp`.

## Order history

`GET /orders/me?page=1&page_size=20` returns `{total,page,page_size,items}`. Page
starts at 1; page size is 1–100 (default 20). `items` contains orders owned by
the authenticated customer's `customer_id`, newest first. An empty history
returns HTTP 200 with `total: 0` and an empty `items` array.

Each order has `id`, `created_at`, `status`, `subtotal_amount`,
`discount_amount`, `total_amount`, `item_count`, `items`, shipping fields
(`shipping_company`, `shipping_method`, `shipping_area`, `shipping_city`,
`shipping_warehouse_number`, `shipping_street`, `building_number`,
`shipping_apartment`, `delivery_address`), `payment_method`, and nullable
`tracking_number`. Item entries include `id`, `product_id`, `quantity`, `price`,
`discount_amount`, `product_name`, `product_sku`, and `total_price`.
`item_count` is the sum of item quantities. Internal sync state/errors and
customer contact fields are not returned.

`GET /orders/me/{order_id}` returns that order in the same shape. Missing and
not-owned IDs both return HTTP 404. Guest orders remain unlinked at checkout;
matching a profile's phone or email does not grant history access. A customer
order is associated only when checkout carries a valid customer bearer token.
There is no automatic guest-order claim/link operation.

## Contact request

`POST /feedback/email` remains compatible with `{name,email,text}`. Optional
`topic` accepts `general`, `order`, `delivery`, `returns`, `product`, or `other`
and defaults to `general`. Optional `orderReference` is a positive order ID and
may be sent only with customer authentication; it must belong to that customer.
Unauthenticated references return 401, and non-owned/missing references return
404. Success is HTTP 202 `{message:"Feedback accepted"}` after SMTP accepts the
message. The configured `FEEDBACK_RECIPIENT_EMAIL`, SMTP sender and enabled
email notifications are required; delivery/configuration failures return 503.
Requests are limited to ten per minute per source IP and five per minute for
each source IP/email pair.

## Shop rules

The backend does not currently hold an approved/versioned legal rules document.
The shop UI's delivery, payment and returns copy remains frontend content; no
backend rules endpoint or acceptance requirement is defined by this API.
