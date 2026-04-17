# Workbook 202 — Design Crop Mixes (Stage A2 sample lane)

Authoritative column mapping lives in [`docs/spreadsheet-academy-map.md`](../../../../docs/spreadsheet-academy-map.md) (tab `Design Crop Mixes`, workbook `202`).

## Operator targets

| Academy surface | Canonical CSV |
|-----------------|---------------|
| Sellable mix rows (`B:F`) | Merge into [`reference/crop_sales_formats.csv`](../../../../docs/historical-import-csv-contracts.md) before recipe lines |
| Ingredient block (`L:N`: Choose Mix, Choose Ingredients, Qty) | [`reference/product_recipe_components.csv`](../../../../docs/historical-import-csv-contracts.md) |

`Parent SKU` in the academy layout is structural and is **not** mapped into `crop_sales_formats` SKU.

## Stage A2 connector

Checked-in wiring:

- **Live pull (with baseline bundle):** `docs/live-rehearsal-baseline-config.example.json` — two tab entries on **`Design Crop Mixes`** (append `B:F` block into `reference/crop_sales_formats.csv`, write `reference/product_recipe_components.csv` from **`L:N`**).
- **Offline rehearsal (no Google):** `design_crop_mixes_tab_sample.csv` + `docs/live-rehearsal-202-mix-snapshot-config.example.json`; run `make live-rehearsal-202-mix-snapshot-validate` (or `scripts/run_202_mix_stage_a2_snapshot_rehearsal.py`). Example validate-only summary totals: `202-mix-snapshot-validate-summary.example.json`.

**Phase 2 exit:** satisfied for §6 — see `docs/phase-2-exit-checklist.md` and `docs/production-decision-log.md` (2026-04-17 Phase 2 Exit entry).

For importer column contracts, see `docs/historical-import-csv-contracts.md` § `product_recipe_components.csv`.
