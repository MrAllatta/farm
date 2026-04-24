# Live Import Lanes

This directory defines the demo-path import lanes.

- `reference.json` pulls shared reference contracts (including channel rollups). It also pulls sheet **402** tabs **Seed Sources** and **Seed Order** from the same workbook as Crop Planner (`1jFhWod…`) into `reference/seed_sources.csv` and `reference/seed_orders.csv` (Stage A2 / `pull_stage_a2_bundle` only — no local CSV overlay). On disk the lane root is `live_import_bundle/reference/` and canonical CSVs live one level deeper in `live_import_bundle/reference/reference/`; **`import_reference_data`** accepts that layout (same as **`import_historical_data`**’s `reference/` resolution).
- `crop-plan.json` pulls operator crop-plan lanes (`plantings.csv`, `nursery_events.csv`). The committed bundle under `farm/data/live_import_bundle/crop-plan/` includes `year_2024/planning_year.csv` and `year_2026/planning_year.csv` (2024 archived, 2026 active, overplant 1.10) so `import_historical_data` can create `PlanningYear` rows on a fresh database without a separate shell bootstrap.
- `sales-plan.json` pulls workbook 301 tab `Sales Plan 302` into `year_YYYY/sales_plan_302.csv`. This lane does **not** ship `planning_year.csv`; run `live-import-crop-plan` (after reference data is loaded) so `PlanningYear` rows exist before sales-plan apply.
- `historical-601.json` pulls **`601 Field Walk & Weekly Sales Plan LSF YYYY`** from the crops parent folder on Drive (`drive_folder_id` in the JSON). Each calendar year must be a spreadsheet whose **exact title** matches that pattern (with `YYYY`). Tabs **`Market`**, **`Orders`**, and **`Build Crop Mix`** normalize to `year_YYYY/sales_events.csv` and `year_YYYY/pack_batch_components.csv`. Run after reference (and usually after crop-plan / sales-plan) so channels, products, and planning years already exist. Configured years are **2023–2025**; cloud **`import_historical_data`** in **`farm-backfill`** still uses **`CLOUD_RUN_IMPORT_START_YEAR`/`CLOUD_RUN_IMPORT_END_YEAR`** (default **2024–2026**), so set **`CLOUD_RUN_IMPORT_START_YEAR=2023`** (and an appropriate end year) on the backfill job if you need **2023** rows applied in Cloud SQL.

**Recommended order on a fresh database:** `make live-import-reference` (or `make import-reference` / `import_reference_data`), then `make live-import-crop-plan`, then `make live-import-sales-plan`, then pull/apply the 601 lane (same `pull_stage_a2_bundle` + `import_historical_data` pattern as other lanes; see `scripts/farm_cloud_pull.sh` / cloud **`farm-backfill`** which runs all four lanes in order).

Run lanes locally:

- `make live-import-reference`
- `make live-import-crop-plan`
- `make live-import-sales-plan`
- `make live-import-all` (reference → crop-plan → sales-plan)

Validation (no network):

- `make live-import-validate`

Engineering rehearsal configs under `docs/live-rehearsal-*.example.json` remain available but are not on the demo path.
