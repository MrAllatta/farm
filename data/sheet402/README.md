# Sheet 402 CSV contract (seed sources / seed order)

Used by `make manage ARGS="import_sheet_402 <dir>"` (see `apps/core/management/commands/import_sheet_402.py`).

## Files

- **`sheet402_seed_sources.csv`** (required): columns must include `Crop`, `Variety`. Optional: `Supplier`, `Catalog Number` or `Catalog`, `Source URL` or `URL`, `Notes`.
- **`sheet402_seed_order.csv`** (optional): columns must include `Crop`, `Variety`, `Season Year`, `Planned Quantity`, `Unit`. Optional: `Notes`.

Crop names must match existing `CropInfo.name` rows. Seed order import requires a matching `PlanningYear` and an existing `Variety` for that crop/name pair.

Example dry run from repo root:

```bash
make manage ARGS="import_sheet_402 farm/data/sheet402 --dry-run"
```
