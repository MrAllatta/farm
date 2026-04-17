# Workbook 202 — Design Crop Mixes (Stage A2 sample lane)

Authoritative column mapping lives in [`docs/spreadsheet-academy-map.md`](../../../../docs/spreadsheet-academy-map.md) (tab `Design Crop Mixes`, workbook `202`).

## Operator targets

| Academy surface | Canonical CSV |
|-----------------|---------------|
| Sellable mix rows (`B:F`) | Merge into [`reference/crop_sales_formats.csv`](../../../../docs/historical-import-csv-contracts.md) before recipe lines |
| Ingredient block (`L:N`: Choose Mix, Choose Ingredients, Qty) | [`reference/product_recipe_components.csv`](../../../../docs/historical-import-csv-contracts.md) |

`Parent SKU` in the academy layout is structural and is **not** mapped into `crop_sales_formats` SKU.

## Stage A2 connector

There is no checked-in Google Sheets config for `202` in this sample folder yet. Add a `pull_stage_a2_bundle` configuration when the operator lane is activated: same declarative rules as other Stage A2 packets (`required_header_set_scan`, column projection per `spreadsheet-academy-map.md`). Export files into a bundle layout matching `docs/data-import-migration.md` so `import_historical_data` can consume `reference/product_recipe_components.csv` in tier 1.

For a minimal offline hand-edit, see column headers and normalization in `docs/historical-import-csv-contracts.md` § `product_recipe_components.csv`.
