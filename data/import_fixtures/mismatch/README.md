## Mismatch Fixture Pack

This fixture pack is intended to exercise preflight mismatch behavior.

Included files:

- `crop_by_season.csv`: mismatch matrix with deterministic outcomes:
  - namespace mismatch (`Unknown Block`) -> row-level `namespace_mismatch` error
  - stale FK (`Missing Crop`) -> row-level `stale_fk` error
  - invalid maturity (`DTM=0`) -> `skipped` outcome
- `crop_info.csv`: contains `Carrot` so stale-FK and skip rows remain isolated and deterministic.
- `blocks.csv`: baseline reference row to keep fixture shape aligned with importer reference expectations.

Expected `CropBySeason` canonical outcomes in validate-only and apply modes:

- `error=2`
- `skipped=1`
