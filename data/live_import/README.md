# Live Import Lanes

This directory defines the demo-path import lanes.

- `reference.json` pulls shared reference contracts (including channel rollups). It also pulls sheet **402** tabs **Seed Sources** and **Seed Order** from the same workbook as Crop Planner (`1jFhWod…`) into `reference/seed_sources.csv` and `reference/seed_orders.csv` (Stage A2 / `pull_stage_a2_bundle` only — no local CSV overlay). On disk the lane root is `live_import_bundle/reference/` and canonical CSVs live one level deeper in `live_import_bundle/reference/reference/`; **`import_reference_data`** accepts that layout (same as **`import_historical_data`**’s `reference/` resolution).
- `crop-plan.json` pulls operator crop-plan lanes (`plantings.csv`, `nursery_events.csv`). The committed bundle under `farm/data/live_import_bundle/crop-plan/` includes `year_2024/planning_year.csv` and `year_2026/planning_year.csv` (2024 archived, 2026 active, overplant 1.10) so `import_historical_data` can create `PlanningYear` rows on a fresh database without a separate shell bootstrap.
- `sales-plan.json` pulls workbook 301 tab `Sales Plan 302` into `year_YYYY/sales_plan_302.csv`. This lane does **not** ship `planning_year.csv`; run `live-import-crop-plan` (after reference data is loaded) so `PlanningYear` rows exist before sales-plan apply.

**Recommended order on a fresh database:** `make live-import-reference` (or `make import-reference` / `import_reference_data`), then `make live-import-crop-plan`, then `make live-import-sales-plan`.

Run lanes locally:

- `make live-import-reference`
- `make live-import-crop-plan`
- `make live-import-sales-plan`
- `make live-import-all` (reference → crop-plan → sales-plan)

Validation (no network):

- `make live-import-validate`

Engineering rehearsal configs under `docs/live-rehearsal-*.example.json` remain available but are not on the demo path.
