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
  - stale FK (`Missing Crop`) -> row-level `stale_fk` error on `crop_sales_formats.crop`
- `crop_info.csv`: contains `Carrot` so stale-FK and skip rows remain isolated and deterministic.
- `blocks.csv`: baseline reference row to keep fixture shape aligned with importer reference expectations.
- `manifest.json`: canonical apply-mode gate expectations for totals, model outcomes, and row-error contract.

Expected canonical outcomes:

- `CropBySeason.error=4` (`2x namespace_mismatch`, `2x stale_fk`)
- `skipped=1`
- plus one additional `stale_fk` error from `CropSalesFormat`
