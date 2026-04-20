## Mismatch Fixture Pack

This fixture pack is intended to exercise preflight mismatch behavior.

Included files:

- `crop_by_season.csv`: mismatch matrix with deterministic outcomes:
  - namespace mismatch (`Unknown Block`) -> row-level `namespace_mismatch` error
  - namespace mismatch (`Unknown Tunnel`) -> row-level `namespace_mismatch` error
  - stale FK (`Ghost Crop`) -> row-level `stale_fk` error on `crop_by_season.crop`
  - stale FK (`Missing Crop`) -> row-level `stale_fk` error
  - invalid maturity (`DTM=0`) -> `skipped` outcome
- `crop_sales_formats.csv`: mixed valid + stale-FK rows:
  - valid `Carrot` product row remains importable in apply mode
  - stale FK (`Ghost Crop`, `Missing Crop`, `Phantom Crop`) -> row-level `stale_fk` errors on `crop_sales_formats.crop`
- `year_2021/plantings.csv`: stale-FK matrix for planning-tier diagnostics:
  - stale FK (`Missing Crop`) -> row-level `stale_fk` error on `plantings.crop`
  - stale FK (`Missing Block`, `Ghost Block`) -> row-level `stale_fk` errors on `plantings.block`
- `year_2021/planning_year.csv`: creates year context so planting mismatch errors isolate to crop/block resolution.
- `crop_info.csv`: contains `Carrot` so stale-FK and skip rows remain isolated and deterministic.
- `blocks.csv`: baseline reference row to keep fixture shape aligned with importer reference expectations.
- `manifest.json`: canonical validate/apply gate expectations for totals, model outcomes, and full row-error contract (`model`, `row`, `code`, `field_path`, `message`).

Expected canonical outcomes:

- `CropBySeason.error=5` (`3x namespace_mismatch`, `2x stale_fk`)
- `Planting.error=3` (`1x plantings.crop`, `2x plantings.block`)
- `skipped=1` in apply mode (`CropBySeason` DTM skip)
- plus three `stale_fk` errors from `CropSalesFormat`
