# Shop price endings — 26 October 2026

## Observed practice and evidence

COMFY's [Odesa home page](https://comfy.ua/ua/odesa/) displayed 549, 649 and 749 UAH offers; its [trimmer category](https://comfy.ua/ua/trimmers/) displayed 799, 999, 2,499 and 3,999 UAH as well as 549 and 659. Its [shaver category](https://comfy.ua/ua/razor/) displayed 3,699, 3,999, 4,699 and 22,999 UAH. These were observed page samples, not a catalogue-wide statistical sample or access to COMFY's internal pricing algorithm. Category pages used the site's default location.

The pattern is broader than an ending of 99: 9-ending prices coexist with price ladders and separate promotion cues. Discounted items display a crossed-out reference and a saving in UAH or percent. A cosmetic price-ending change alone does not justify inventing a discount badge or an old price.

Thomas & Morwitz's [2005 experiments](https://academic.oup.com/jcr/article-abstract/32/1/54/1796360?login=false) explain the left-digit effect, especially when the leftmost digit changes (500 → 499). This does not establish the same effect for 555 → 549. Anderson & Simester's [field experiments](https://doi.org/10.1023/A:1023581927405), listed by [the author at MIT](https://mitmgmtfaculty.mit.edu/dsimester/research-publications/), found demand effects for 9 endings that varied by context and sale cues. Neither study establishes an optimal price or guaranteed uplift for our Ukrainian grooming catalogue.

## Chosen policy

- Suggest the closest lower **whole-UAH price ending in 9**.
- Never increase the entered price. Do not reduce it by more than **2%**.
- 500 → 499; 555 → 549; 544 → 539; 1,000 → 999; 1,097 → 1,089.
- Keep an existing suitable price. Keep 115 and 145 because the next lower ending exceeds the 2% limit. Leave zero, missing and invalid values alone.
- The 2% limit is a conservative merchandising decision, not a research result. It limits price reductions; it does not guarantee a margin or optimal conversion.
- Apply only to `shop_price`. Supplier/base price and recommended retail price remain independent; orders retain their historical price snapshots. Existing promotions continue to use the shop price normally, without additional rounding.
- In backoffice the suggestion updates as the operator types. Only the explicit apply button changes the entered value; normal save persists the chosen value. Manual choices remain allowed.

## Catalogue update and verification

The local database contained 552 products. All were reviewed; 443 shop prices were lowered, 85 already ended in 9, 22 were retained by the 2% guard, and 2 had zero prices (product IDs 929 and 939). The maximum actual reduction was 9 UAH. All resulting shop prices remained at or above the stored base price; that base field is not a complete margin calculation.

Before applying, a full custom-format PostgreSQL backup was saved and its archive listing checked. A JSON audit records every product's old/new shop price, base price and RRP. The command writes that audit before committing and rolls back if it cannot create it. Reports describe the planned/apply run; successful exit and the separate verification file confirm persistence.

Local evidence is under `output/pricing/2026-10-26/`: `preview.json`, `applied.json`, `before-verification.json`, `verification.json`, and the `before-price-endings-*.dump` backup. The all-products checksum excluding `shop_price` and its update timestamp was identical before/after. All 552 prices matched the audit, RRP/base prices were unchanged, and a second recommendation pass produced no further changes.

The running public API returned product 1151 with `shop_price`, `base_price` and effective `price` 749.00 instead of 752.00, retaining RRP 752.00.

## Maintenance

Preview: `POSTGRES_HOST=127.0.0.1 python -m scripts.recommend_shop_prices --report NEW_PATH.json`

Apply after backup/review: add `--apply`. Paths must be new and their parent directory must already exist. Apply locks product rows and only changes qualifying shop prices. Re-running immediately is idempotent. Supplier imports still preserve manual shop prices; new products use the existing creation defaults and can be adjusted with the backoffice suggestion.

For production, pass `--expected-preview REVIEWED_PREVIEW.json` on apply. A short
table lock with a five-second lock timeout fences concurrent product writes,
including inserts, while the complete audited identity/price vector is compared.
Any drift aborts before changes commit. The intent report and its parent directory
are synced before commit. A report alone never proves successful commit: after
an error or lost connection, verify persisted prices before deciding on recovery.
Never blindly repeat an uncertain apply or restore the whole live database.

Python policy: `app/utils/shop_price_recommendation.py`. App-local TypeScript counterpart: `sc-fe/apps/backoffice/utils/shopPriceRecommendation.ts`. Keep their money-boundary examples aligned when changing the policy. Tests cover the 2% boundary, invalid money, never increasing, idempotence, preview/apply, and rollback when audit creation fails.
