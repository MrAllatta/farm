# Sheet 402 CSV contract (seed sources / seed order)

Used by `make manage ARGS="import_sheet_402 <dir>"` (see `apps/core/management/commands/import_sheet_402.py`).

## Files

- **`sheet402_seed_sources.csv`** (required): columns must include `Crop`, `Variety`. Optional: `Supplier`, `Catalog Number` or `Catalog`, `Source URL` or `URL`, `Notes`.
- **`sheet402_seed_order.csv`** (optional; **`sheet402_seed_orders.csv`** is accepted too): columns must include `Crop`, `Variety`, `Season Year`, `Planned Quantity`, `Unit`. Optional: `Notes`, `Supplier`, `Catalog Number` / `Catalog`, `Source URL` / `URL`, `Variety Notes`.

Crop names must match existing `CropInfo.name` rows. Seed order import requires a matching `PlanningYear`. If no `Variety` exists yet for the crop/name pair, one is created from the order row (optional columns: `Supplier`, `Catalog Number` / `Catalog`, `Source URL` / `URL`, `Variety Notes` — line `Notes` stay on `SeedOrder` only).

Example dry run from repo root:

```bash
make manage ARGS="import_sheet_402 farm/data/sheet402 --dry-run"
```
