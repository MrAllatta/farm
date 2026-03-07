# Farm Planning: One-Time Data Import Workflow Research

## Current State (as of 2026-03-07)

### Existing Import Infrastructure
The codebase has a single Django management command framework for data import:

**Location:** `apps/core/management/commands/import_reference_data.py`

#### Current Capabilities
- **Blocks** → `reference.Block` (farm layout: fields, high tunnels, greenhouses)
- **Crops** → `reference.CropInfo` (crop metadata, nursery/harvest parameters)
- **Crop by Season** → `reference.CropBySeason` (field block_type-specific growing profiles)
- **Sales Channels** → `reference.SalesChannel` (market days, weekly targets, allocation priority)

#### Key Features
✓ CSV input via `DictReader` with flexible parsing  
✓ Type/family mapping with known data fixes for data quality issues  
✓ Duplicate detection (keeps first occurrence, warns on duplicates)  
✓ Helper methods for robust numeric parsing: `_int()`, `_dec()`, `_int_or_none()`, `_dec_or_none()`  
✓ Dry-run mode (`--dry-run`) for validation without saving  
✓ Error tracking with per-row error messages and summary counts  

#### Pattern
```python
# Typical import flow:
1. Load CSV from file system
2. Iterate rows with csv.DictReader
3. Validate/transform fields
4. Handle nulls and type conversions
5. Skip invalid rows, track errors
6. Use update_or_create() for idempotency
```

---

## Task: Import Five Years of Historical Data

### Scope
- **Timespan:** 5 years of accumulated farm operations data (2021–2025, e.g.)
- **Source:** CSV/GSheets exports (not yet available for review)
- **Goal:** Populate Django models with historical plantings, harvests, field observations, sales

### Models to Import (Priority Order)

#### Tier 1: Reference Data (Already partially covered)
- [x] `reference.Block` ← blocks.csv
- [x] `reference.CropInfo` ← crop_info.csv
- [x] `reference.CropBySeason` ← crop_by_season.csv
- [x] `reference.SalesChannel` ← sales_channels.csv
- [ ] `reference.CropSalesFormat` ← **NEW** (crop × sale price/unit)

#### Tier 2: Planning Data
- [ ] `planning.PlanningYear` (year, status, overplant_factor)
- [ ] `planning.Planting` (crop × block × dates, planned/actual)
- [ ] `planning.NurseryEvent` (seed/pot/hardened/transplant with dates, tray counts, germination)
- [ ] `planning.HarvestEvent` (weekly harvest events with planned/actual quantities, bins, labor)

#### Tier 3: Operations Data
- [ ] `operations.FieldWalkNote` (crop condition observations, adjusted dates, yield adjustments)
- [ ] `operations.InventoryLedger` (harvest in, sale out, waste, transfers, running balance)
- [ ] `operations.PackAllocation` (harvest → sales channel allocation)

#### Tier 4: Sales Data
- [ ] `sales.SalesEvent` (per-product sales: brought, sold, returned, revenue)
- [ ] `sales.QuickSalesEntry` (quick cash/card entry for markets without detailed sales)

#### Tier 5: Core Data
- [ ] `core.RotationHistory` (family → block → year, populated by clone_plan or manual entry)
- [ ] `core.RotationRule` (botanical family → min gap years) - **Reference/config only**

---

## Data Dependencies & Import Order

```
┌─────────────────────────────────────────┐
│ REFERENCE (Independent)                  │
├─────────────────────────────────────────┤
│ Block, CropInfo, SalesChannel            │ ← Import first
│ CropBySeason (depends on CropInfo)       │
│ CropSalesFormat (depends on CropInfo)    │
└─────────────────────────────────────────┘
         ↓ (Foreign Keys ↓)
┌─────────────────────────────────────────┐
│ PLANNING (Depends on Reference)          │
├─────────────────────────────────────────┤
│ PlanningYear (independent)               │
│ Planting (FK: CropInfo, CropBySeason,    │ ← Import second
│           Block, PlanningYear)           │
│ NurseryEvent (FK: Planting)              │
│ HarvestEvent (FK: Planting)              │
└─────────────────────────────────────────┘
         ↓ (Foreign Keys ↓)
┌─────────────────────────────────────────┐
│ OPERATIONS (Depends on Planning)         │
├─────────────────────────────────────────┤
│ FieldWalkNote (FK: Planting)             │ ← Import third
│ InventoryLedger (FK: CropInfo,           │
│                  HarvestEvent optional)  │
│ PackAllocation (FK: HarvestEvent,        │
│                 InventoryLedger,         │
│                 SalesChannel,            │
│                 CropSalesFormat)         │
└─────────────────────────────────────────┘
         ↓ (Foreign Keys ↓)
┌─────────────────────────────────────────┐
│ SALES (Depends on Reference)             │
├─────────────────────────────────────────┤
│ SalesEvent (FK: SalesChannel,            │ ← Import fourth
│             CropSalesFormat optional)    │
│ QuickSalesEntry (FK: SalesChannel)       │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ CORE (Mostly post-process)               │
├─────────────────────────────────────────┤
│ RotationHistory (FK: Block)              │ ← Populate from
│                                          │    Planting data or
│                                          │    clone_plan cmd
└─────────────────────────────────────────┘
```

---

## Model Structure Summary

### reference.Block
```python
name (unique, PK)
block_type: 'field' | 'high_tunnel' | 'greenhouse'
num_beds
bed_width_feet
bedfeet_per_bed
walk_route_order (ordering for field walks)
```
**Derived:** `total_bedfeet` (property), `square_feet` (property)

### reference.CropInfo
```python
name (unique, PK) — e.g., "Tomato Beefsteak", "Lettuce Mix"
crop_type — e.g., "Tomatoes", "Greens", "Roots"
botanical_family — for crop rotation
propagation_type: 'seed' | 'vegetative_clove' | 'vegetative_tuber' | 'vegetative_slip'
is_perennial
fresh_or_storage: 'fresh' | 'storage'
storage_weeks
can_hold_in_field
harvest_unit — "pounds", "bunches", "each", etc.
avg_unit_weight
units_per_bin (e.g., 20 lettuce heads per crate)
harvest_bin — "Crate", "Tote", "Flat", etc.
harvest_tools — "serrated knife", "snaps", etc.
harvest_rate_per_hour (units/hour for labor planning)
nursery_weeks (0 if direct seeded)
weeks_until_pot_up
pot_up_tray_size — e.g., 72, 128 (cells)
seeded_tray_size
seeds_per_cell
thinned_plants (final plant count per cell after thinning)
seeds_per_ounce (for bulk seed)
```

### reference.CropBySeason
```python
crop (FK: CropInfo)
block_type (unique together with crop)
field_week_start (1–52 ISO week)
field_week_end
total_yield_per_bedfoot (lbs/bedfoot, e.g., 0.5–10)
harvest_weeks (duration)
dtm_days (days to maturity from plant date)
rows_per_bed
ds_seed_rate (seeds per row foot for direct seed)
tp_inrow_spacing (feet, for transplants)
seeder_settings — machine parameters
trellis_system — "cattle panel", "twine", etc.
mulch — "straw", "landscape fabric", etc.
row_cover — "Agribon 30", "none", etc.
irrigation — "drip", "overhead", "none"
```
**Derived:** `wtm_weeks`, `weekly_yield_per_bedfoot`

### reference.CropSalesFormat
```python
crop (FK: CropInfo)
product_name — e.g., "Tomato Beefsteak Box", "Lettuce Mix Clamshell"
sale_price — $ per unit
sale_unit — "each", "pound", "bunch", "pint", "bag"
harvest_qty_per_sale_unit — conversion (e.g., 2 bunches per sale unit)
sku — barcode/sku for POS
is_active
```

### reference.SalesChannel
```python
name — "Farmers Market", "CSA", "Wholesale", etc.
days_of_week — PostgreSQL ArrayField: ["Saturday", "Sunday"]
start_week (ISO week, 1–52)
end_week
weekly_target ($ or revenue goal)
is_csa (flag for subscription model)
allocation_priority (1 = highest)
```
**Derived:** `num_weeks`, `annual_target`

### planning.PlanningYear
```python
year (unique, PK)
status: 'planning' | 'active' | 'complete' | 'archived'
overplant_factor (decimal, e.g., 1.10 for 10% overplanting)
```

### planning.Planting
```python
planning_year (FK: PlanningYear)
crop (FK: CropInfo)
crop_season (FK: CropBySeason) — determines DTM, yield/bedfoot, etc.
variety (e.g., "San Marzano")
block (FK: Block)
bed_start, bed_end (inclusive range, 1–num_beds)
succession_group (identifier for multiple sowings of same crop)
revision_of (FK: self, null, for revisions of earlier plantings)

# PLANNED (input)
planned_bedfeet
planned_plant_date
planned_first_harvest_date (auto-calculated: plant_date + DTM)
planned_last_harvest_date (auto-calc: first_harvest + harvest_weeks)
planned_total_yield (auto-calc: planned_bedfeet × yield/bedfoot)

# ACTUAL (recorded during season)
actual_bedfeet (null until executed)
actual_plant_date
actual_first_harvest_date
actual_last_harvest_date
actual_total_yield

status: 'planned' | 'seeded' | 'planted' | 'growing' | 'harvesting' | 'complete' | 'failed' | 'skipped' | 'revised'
notes
created_at, updated_at
```
**Methods:**
- `save()` — auto-calculates planned dates/yield if not provided
- `generate_nursery_events()` — creates seed/pot/transplant events
- `generate_harvest_events()` — creates weekly harvest events

### planning.NurseryEvent
```python
planting (FK: Planting)
event_type: 'seed' | 'pot_up' | 'harden' | 'transplant'
planned_date
planned_tray_count
planned_tray_size (e.g., 72, 128)
actual_date (null until recorded)
actual_tray_count
actual_tray_size
actual_germination_rate (%)
notes
```

### planning.HarvestEvent
```python
planting (FK: Planting)
planned_date (e.g., week by week)
planned_quantity
planned_units (matches crop.harvest_unit)
actual_date (null until harvested)
actual_quantity
actual_units
actual_bins (bin count)
actual_bin_type (e.g., "Tote", "Crate")
actual_hours (labor hours)
actual_workers
quality_grade: 'prime' | 'seconds' | 'mixed'
notes
```
**Method:** `record_bins(bin_count, bin_type=None)` — converts bins to quantity using crop.units_per_bin

### operations.FieldWalkNote
```python
planting (FK: Planting)
walk_date
condition: 'good' | 'fair' | 'poor' | 'failed'
adjusted_first_harvest_date (null if no change)
adjusted_last_harvest_date
yield_adjust_pct (100 = no change, 50 = half expected)
notes
```

### operations.InventoryLedger
```python
crop (FK: CropInfo)
harvest_event (FK: HarvestEvent, null for non-harvest events)
event_date
event_type: 'harvest_in' | 'sale_out' | 'return_in' | 'waste_out' | 'transfer' | 'quality_check' | 'year_end_count' | 'adjustment'
quantity (signed: positive = in, negative = out)
running_balance (auto-calculated on save)
expiry_date (null for fresh market items)
storage_location (e.g., "Cold Room 1", "Floor Storage")
notes
created_at
```
**Auto-calc:** `running_balance` = previous balance + quantity

### operations.PackAllocation
```python
harvest_event (FK: HarvestEvent, null)
inventory_draw (FK: InventoryLedger, null)
channel (FK: SalesChannel)
product (FK: CropSalesFormat)
pack_date
quantity
notes
```

### sales.SalesEvent
```python
channel (FK: SalesChannel)
product (FK: CropSalesFormat, null for quick-entry aggregates)
sale_date
planned_quantity (null)
planned_revenue (null)
actual_quantity (null)
actual_revenue (null)
actual_price (per unit)
brought_quantity (units brought to market)
returned_quantity (units not sold)
notes
```
**Property:** `sell_through_pct`, `sale_week`

### sales.QuickSalesEntry
```python
channel (FK: SalesChannel)
sale_date
total_cash
total_card
notes
```
**Property:** `total_revenue`, unique_together = [channel, sale_date]

### core.RotationHistory
```python
block (FK: Block)
year
botanical_family
notes
unique_together = [block, year]
```

### core.RotationRule
```python
botanical_family (unique)
min_gap_years
```

---

## Export Command as Template

The `export_season.py` command provides a working template for CSV export:

```python
# Generic export pattern:
def _export_model(self, queryset, fields, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for obj in queryset:
            row = {}
            for field in fields:
                val = getattr(obj, field, None)
                if callable(val):
                    val = val()
                row[field] = val
            writer.writerow(row)
```

**Import will be the inverse:** field values → object attributes → save to DB.

---

## Challenges & Considerations

### Data Quality Issues
- **Nulls & Defaults:** Many fields are optional (null=True, blank=True). Import must handle missing values gracefully.
- **Duplicates:** `CropInfo.name` is unique; duplicate crops should merge or skip.
- **Type Mismatches:** CSV exports often contain formatting artifacts (e.g., "$1,200" for decimals, "na" for nulls).
- **FK Resolution:** Plantings reference `crop_name` (string) not crop_id; must resolve crop by name first.

### Date Handling
- **ISO Week Consistency:** `planned_plant_date` must be real dates, not week numbers. If CSV has "Week 20", must convert to actual date.
- **Year Boundaries:** Harvest/inventory may span Jan–Mar of next calendar year but be part of same planning year.

### Calculated Fields
- **Auto-calc in save():** `Planting.planned_first_harvest_date` is auto-calculated from DTM if not provided.
- **Running balance:** `InventoryLedger.running_balance` is auto-calculated from sequence of events.
- **Derived properties:** Don't import these; let the model calculate them.

### Import Idempotency
- Use `update_or_create()` for reference data (crops, blocks, channels).
- For transactional data (plantings, harvests, sales), may need to decide: skip duplicates, merge, or error.

### Constraints
- **Unique constraints:** Some models have `unique=True` or `unique_together`.
  - `CropInfo.name`, `Block.name`, `SalesChannel.unique_together = [??]` (no explicit constraint; name is implicit)
  - `CropBySeason.unique_together = [crop, block_type]`
  - `OperationField.unique_together = [block, year]`
  - `SalesQuickEntry.unique_together = [channel, sale_date]`

---

## Next Steps: Build Import Command

### Structure
Create `apps/core/management/commands/import_historical_data.py`:

```python
class Command(BaseCommand):
    help = "Import 5 years of historical farm data from CSV"
    
    def add_arguments(self, parser):
        parser.add_argument("data_dir", type=str, help="Directory with year_YYYY subdirs")
        parser.add_argument("--start-year", type=int, default=2021)
        parser.add_argument("--end-year", type=int, default=2025)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--verbose", action="store_true")
    
    def handle(self, *args, **options):
        # 1. Import reference data (once)
        self._import_reference_data(options["data_dir"])
        
        # 2. For each year:
        for year in range(options["start_year"], options["end_year"] + 1):
            year_dir = os.path.join(options["data_dir"], f"year_{year}")
            self._import_year_data(year_dir, year, options["dry_run"])
    
    def _import_reference_data(self, data_dir):
        # Import blocks, crops, crop_by_season, channels
        # (can reuse existing logic from import_reference_data.py)
        pass
    
    def _import_year_data(self, year_dir, year, dry_run):
        self._import_planning_year(year_dir, year, dry_run)
        self._import_plantings(year_dir, year, dry_run)
        self._import_nursery_events(year_dir, year, dry_run)
        self._import_harvest_events(year_dir, year, dry_run)
        self._import_field_walk_notes(year_dir, year, dry_run)
        self._import_sales_data(year_dir, year, dry_run)
        self._import_inventory(year_dir, year, dry_run)
```

### CSV Schema Assumptions
Assuming source has these files in `data_dir/` or `data_dir/year_YYYY/`:

**Reference:**
- `blocks.csv` (already defined)
- `crop_info.csv` (already defined)
- `crop_by_season.csv` (already defined)
- `sales_channels.csv` (already defined)
- `crop_sales_formats.csv` — columns: crop_name, product_name, sale_price, sale_unit, harvest_qty_per_sale_unit, sku, is_active

**Per Year (e.g., `year_2023/`):**
- `plantings.csv` — crop_name, variety, block_name, bed_start, bed_end, planned_plant_date, ...
- `nursery_events.csv` — planting_id, event_type, planned_date, actual_date, ...
- `harvest_events.csv` — planting_id, planned_date, planned_quantity, actual_date, ...
- `field_walk_notes.csv` — planting_id, walk_date, condition, yield_adjust_pct, ...
- `sales_events.csv` — channel_name, product_name, sale_date, actual_quantity, actual_revenue, ...
- `quick_sales_entries.csv` — channel_name, sale_date, total_cash, total_card, ...
- `inventory_ledger.csv` — crop_name, event_date, event_type, quantity, storage_location, ...

---

## Questions for User

Before building the full import command, need clarification on:

1. **CSV Format:** What is the actual structure of the source data? (e.g., are years in separate folders, combined, or in GSheets tabs?)
2. **Date Format:** How are dates represented in the CSV? (ISO YYYY-MM-DD, MM/DD/YYYY, week numbers, etc.)
3. **ID vs. Names:** Do CSVs use database IDs (e.g., planting_id, product_id) or human-readable names (crop_name, product_name)?
4. **Foreign Keys:** For transactional data (plantings, sales), how are relationships represented?
5. **Scope:** Is all data available, or are some datasets (e.g., detailed field walk notes) sparse/partial?
6. **Conflict Resolution:** If historical data overlaps with existing data, should we skip, merge, or error?

---

## Summary

| Component | Status | Notes |
|-----------|--------|-------|
| Reference import framework | ✓ Exists | `import_reference_data.py` covers blocks, crops, seasons, channels |
| Historical year structure | ○ Design needed | Likely `data_dir/year_YYYY/` per year |
| Planting import | ○ To build | Depends on CSV schema; likely FK resolution by name |
| Nursery event import | ○ To build | Straightforward; mostly dates and counts |
| Harvest event import | ○ To build | Straightforward; planned vs. actual tracking |
| Field walk import | ○ To build | Condition + yield adjustment % |
| Sales event import | ○ To build | Channel + product lookups; null for quick-entry |
| Inventory ledger import | ○ To build | Running balance auto-calc; tricky with partial history |
| Error handling & validation | ○ To build | Reuse patterns from `import_reference_data.py` |
| Dry-run mode | ✓ Pattern exists | Already in `import_reference_data.py` |

