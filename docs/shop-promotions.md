# Shop product promotions

`ShopPromotion` is the product-discount domain. It is intentionally separate from `Promotion`, which remains the Soul Cuts customer and service-promotion domain.

## Backoffice API

All paths are under `/api/v1` and require a superuser:

- `GET /backoffice/shop-promotions` lists promotions. It supports `product_id`, `category_id`, `brand_id`, `status` (or `period`), `trigger`, `is_active`, and `search` filters.
- `POST /backoffice/shop-promotions` creates a promotion.
- `GET /backoffice/shop-promotions/{id}` reads one promotion.
- `PATCH /backoffice/shop-promotions/{id}` replaces the validated promotion fields and applicability scope.
- `DELETE /backoffice/shop-promotions/{id}` deactivates the promotion (`is_active=false`); it does not delete historical order snapshots.
- `POST /backoffice/shop-promotions/preview` calculates the proposed impact without saving it. It returns affected products, base/new prices, discount amounts, the current and resulting promotion, and conflicts.

Applicability must be either `applies_to_all_products=true` or contain at least one existing `product_id`, `category_id`, or `brand_id`. `include_subcategories` controls category descendant matching.

Supported discount types are `percent`, `fixed_amount`, and `fixed_price`. Supported triggers are `automatic` and `promocode`. Codes are normalized to uppercase.

## Time and state

`starts_at` is inclusive and `ends_at` is exclusive. Naive API datetimes are interpreted in `Europe/Kyiv`; timezone-aware values are normalized to that timezone. `ends_at` must be later than `starts_at`. Responses expose `status` as `scheduled`, `active`, `expired`, or `disabled`.

## Pricing and usage

Shop discounts do not stack. For each product, the shared catalog/quote/checkout pricing service selects the candidate with the lowest final price, then uses the lowest `priority`, then the lowest promotion id as deterministic tie-breakers.

Automatic discounts do not support usage limits. The API rejects `usage_limit` and `usage_limit_per_customer` for them instead of silently ignoring those fields. Promocode limits are counted from non-cancelled orders; checkout locks the promocode promotion row while checking the count and inserting the order.

Order items store `base_price`, `price`, `discount_amount`, `shop_promotion_id`, `promotion_name`, `promotion_code`, and `total_price` as snapshots. Promotion edits or deactivation therefore cannot recalculate historical order prices.
