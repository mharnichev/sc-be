# Inventory backoffice API

All endpoints are under `/api/v1/backoffice/inventory` and require an admin bearer token. They are intentionally private: purchase cost, allocation, procurement, and movement history are not exposed by shop endpoints.

## Stock semantics

`on_hand` is the physical stock (`Product.stock_quantity`). `reserved` is stock allocated to pending order items. `available` is `on_hand - reserved` and is non-negative. `allow_backorder` is the product policy consumed by order allocation; it does not make this endpoint a mutable stock override. There is no endpoint that overwrites stock directly.

`GET /stock` is paginated (`page`, `page_size`, optional `search`). `GET /products/barcode/{barcode}` resolves a scanned barcode. `PATCH /products/{id}/settings` updates only the inventory barcode and backorder policy. `GET /products/{id}/movements` returns the paginated immutable movement ledger.

## Procurement and receipts

`GET /procurement?status=…` returns the aggregated order-item demand queue. Repeat `status` to filter multiple statuses. `POST /procurement/mark-ordered` accepts `{ "order_item_ids": [1] }` and progresses those items.

Receipts follow `draft → posted`. Create a draft with `POST /receipts`, add scans with `POST /receipts/{id}/items`, inspect via `GET /receipts/{id}`, then post with `POST /receipts/{id}/post`. Repeating an item call deliberately increments that receipt line; quantity must always be positive. An item can identify a product by `product_id` or `barcode`, and may allocate received units to order items.

## Manual operations and counts

`POST /operations/opening-balance`, `/receipt`, `/customer-return`, and `/write-off` record a single positive scanned quantity and mandatory reason. They accept `product_id` or `barcode`; optional order references preserve traceability. The movement type determines the stock direction, so clients do not send signed quantities.

Counts follow `draft → posted`. Create with `POST /counts`, upsert each scanned quantity through `PUT /counts/{id}/items`, inspect with `GET /counts/{id}`, and finalize with `POST /counts/{id}/post`. `counted_quantity` may be zero; a count does not alter stock until posting.

## Idempotency and privacy

Mutating state-transition requests require an `Idempotency-Key` header: receipt creation/posting, each manual operation, count creation, and count posting. Reuse the same key only for a retry of the same intent; the service returns the original committed result atomically. Reasons are mandatory for manual operations and counts for auditability. API responses remain scoped to authenticated administrators and should not be copied into public catalog or customer-facing views.
