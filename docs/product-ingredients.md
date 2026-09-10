# Product composition (Склад)

`products.ingredients` is nullable text, exposed in public and backoffice product
responses and editable through product creation/update and the backoffice form.
It contains plain text, including source qualifications and batch notes. `null`
means that no composition has been provided. Some descriptions supply only key
ingredients; those retain their explicit qualification, rather than claiming to
be a complete INCI list.

After deploying migration `0070_product_ingredients`, backfill each environment:

```sh
python -m app.utils.backfill_product_ingredients --report output/ingredients-preview.json
python -m app.utils.backfill_product_ingredients --apply --report output/ingredients-applied.json
```

Review the preview before applying. The utility scans all products, including
hidden ones, copies only explicitly labelled composition sections and preserves
descriptions. Conflicting formulas with no unique product-name match are reported
as ambiguous. Existing non-null values are never overwritten, and conditional
updates skip products whose description changed after reading it.

The XLSX importer also extracts composition for new products or an unset field;
it preserves existing ingredients, including manual edits. A PATCH with
`ingredients: null` explicitly clears the field; omitting it leaves it unchanged.

The shop adapter exposes `ingredients` for a future separate composition block.
The original description is intentionally retained until that block is introduced.
