# Shop sale catalog filter

The following public GET endpoints accept optional boolean `has_discount`:

- `/api/v1/public/products`
- `/api/v1/public/categories/{category_slug}/products`
- `/api/v1/public/categories/{category_slug}/filters`

`true` selects publicly visible products whose current effective public price is
strictly below the base price used by `ShopProductResponse.compare_at_price`.
`false` selects the complementary visible set. Omitting the parameter retains
the existing behavior. Invalid boolean values return 422.

Pricing is calculated once for the candidate set using the existing shared
promotion service. Active automatic promotions apply; disabled, expired, future,
nonmatching promotions and unapplied coupon promotions do not. RRP and a lower
manually entered shop price do not independently establish a discount: the
existing response contract does not present those as a crossed-out base price.
There is no separate persistent sale/compare-at field in the current product model.

Discount membership is evaluated before total counting and pagination, and
composes with existing category, brand, search, popularity and stock filters.
Availability-first ordering is preserved, including effective-price sorting.
The general product list additionally accepts effective-price bounds
`priceMin` / `priceMax` or `price_min` / `price_max`, matching category listings.
Camel-case aliases take precedence; a reversed range returns 422.

Category facets and their price range use the same sale subset when passed
`has_discount`. As before, facets describe the base category subset rather than
applying the currently selected individual facets.

Example: `/api/v1/public/products?has_discount=true&limit=20&offset=0`.
The frontend must send the flag both to the listing and category facet request.
This implementation follows the existing in-memory effective-price sorting
pattern and loads all matching candidates when discount filtering is requested.
No database migration, production data change or deployment is required.
