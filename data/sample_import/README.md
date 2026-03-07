# Sample Import Data Structure

This directory contains realistic sample CSV files for testing the one-time data import workflow. The data represents two complete farm seasons (2023 and 2024).

## Directory Structure

```
data/sample_import/
├── README.md (this file)
├── blocks.csv
├── crop_info.csv
├── crop_by_season.csv
├── sales_channels.csv
├── crop_sales_formats.csv
├── year_2023/
│   ├── planning_year.csv
│   ├── plantings.csv
│   ├── nursery_events.csv
│   ├── harvest_events.csv
│   ├── field_walk_notes.csv
│   ├── inventory_ledger.csv
│   ├── sales_events.csv
│   └── quick_sales_entries.csv
└── year_2024/
    ├── planning_year.csv
    └── plantings.csv (planned plantings only)
```

## Reference Data (Shared Across All Years)

### blocks.csv
- **4 farm blocks:** North Field, South Tunnel, East Greenhouse, West Pasture
- Block characteristics: type, bed count, dimensions, walk order
- Used to layout plantings

### crop_info.csv
- **12 crops:** tomatoes, lettuce, spinach, peppers, roots, cucumber, basil, garlic, potato, asparagus
- Comprehensive metadata: propagation type, nursery weeks, harvest units, units per bin
- Data quality notes: includes realistic variations (some "na" values, decimal formats)

### crop_by_season.csv
- **20 crop × block_type combinations**
- Each defines: DTM days, yield/bedfoot, harvest weeks, spacing, mulch/trellis/irrigation
- Multiple profiles per crop for different growing conditions (field vs. tunnel)

### sales_channels.csv
- **6 sales channels:** Farmers Market Saturday/Sunday, CSA, Wholesale, Farm Store, Restaurant
- Channel metadata: operating weeks, weekly target revenue, priority

### crop_sales_formats.csv
- **20 product SKUs** (crop-specific products with pricing)
- Examples: "Tomato Beefsteak Box (2 lb) @ $8.50", "Lettuce Mix Clamshell @ $3.99"
- Includes harvest-to-sale-unit conversions

## 2023 Season Data (Complete Historical Year)

### planning_year.csv
- Single row: year=2023, status=complete, overplant_factor=1.10

### plantings.csv
- **12 plantings** with full planned AND actual data
- Examples:
  - P001: Tomato Beefsteak (San Marzano) in North Field, completed with yield data
  - P004/P005: Lettuce succession plantings (spring and summer)
  - P011: Garlic planted fall 2022, harvested summer 2023
  - P012: Potato spring planting with mature harvest

**Realistic features:**
- Actual yield variations vs. planned (some over, some under)
- Different status: all 12 marked as "complete" for 2023
- Notes capture observations ("Early planting - good yields", "Succession planting")
- Bed ranges show spatial layout (e.g., beds 1-4 for first tomato, 5-8 for second)

### nursery_events.csv
- **18 rows** covering seed/pot-up/transplant events
- Shows tray counts, germination rates, actual dates vs. planned
- Links to plantings via Planting ID

**Example flow (P001 tomato):**
1. Seed: 2023-03-21 planned → 2023-03-22 actual, 6×128 trays, 0.95 germination
2. Pot-up: 2023-04-25 planned → 2023-04-26 actual, 6×72 trays
3. Transplant: 2023-05-15 planned → 2023-05-17 actual (field planting)

### harvest_events.csv
- **~145 rows** of weekly harvest events from July through November 2023
- Shows progression from peak (P001 week 1: 145 lbs actual) to season end
- Tracks: quantity, units, bins, labor hours/workers, quality grade
- **Real data patterns:**
  - Early season: high quality (prime/excellent)
  - Mid-season: peak yield, mostly prime
  - Late season: quality decline (seconds), lower quantities
  - Labor hours scale with yield

**Example (P001 Tomato Beefsteak weekly harvest):**
```
Week 1:  150 planned → 145 actual, 7 totes, 4.5 hours, 2 workers, prime ✓
Week 4:  150 planned → 155 actual, 8 totes, 4.5 hours, 2 workers, prime (exceeded!)
Week 10: 150 planned → 112 actual, 5 totes, 2.75 hours, 1 worker, seconds (decline)
Week 16: 150 planned → 18 actual, 1 tote, 0.5 hours, 1 worker, seconds (end of season)
```

### field_walk_notes.csv
- **29 rows** of in-season field observations
- Tracks crop condition (good/fair/poor/failed) and yield adjustments
- Some entries adjust harvest dates (e.g., lettuce moved earlier due to heat)

**Examples:**
- P001 (Tomato): Noted fair condition on Aug 20 (95% yield adjustment due to early blight)
- P004 (Lettuce Spring): Poor condition on June 10, harvest truncated from planned 2023-06-13 to actual 2023-06-10 (heat stress → bolting, 50% yield loss)
- P007 (Pepper): Noted ahead of schedule on May 28 (105% yield potential)

### inventory_ledger.csv
- **~180 rows** tracking stock movements for 8 key crops
- Event types: harvest_in, sale_out, waste_out
- Chronologically ordered to support running balance calculation

**Example flow (Tomato Beefsteak):**
```
2023-07-31: harvest_in +145 lbs → balance: 145
2023-07-31: harvest_in +152 lbs → balance: 297 (stacked daily harvests)
2023-08-28: sale_out -150 lbs → balance: 147 (farmer's market)
2023-11-15: waste_out -15 lbs → balance: low (spoilage)
```

### sales_events.csv
- **33 rows** of detailed product-level sales
- Links to channels and crop_sales_formats
- Tracks: brought quantity, sold quantity, returned quantity, revenue

**Example (Farmers Market Saturday, 2023-08-05):**
- Tomato Beefsteak Box: brought 10, sold 8, returned 2 → $68 revenue
- Basil Bunch: brought 12, sold 10, returned 2 → $39.90 revenue
- Total market day: ~$331 (multiple products)

### quick_sales_entries.csv
- **9 rows** of summary-only sales (no product breakdown)
- Farm Store daily cash/card totals
- Simpler data model for venues tracking only total revenue

---

## 2024 Season Data (Planned Year in Progress)

### planning_year.csv
- Single row: year=2024, status=active, overplant_factor=1.15

### plantings.csv
- **10 plantings** with PLANNED data only (no actuals yet)
- Status: all marked as "planned" (season has just begun)
- Plant dates range from Feb–May 2024
- All actual_* fields are null/empty

**Purpose:** Demonstrates how import handles incomplete seasons.

---

## Key Data Patterns for Testing

### 1. Foreign Key Resolution
- **Plantings reference crops and blocks by name** (not ID)
  - `crop_name="Tomato Beefsteak"` must resolve to `CropInfo` object
  - `block_name="North Field"` must resolve to `Block` object
  - Import must do lookups before saving

### 2. Calculated Fields
- **Planting model** auto-calculates planned_first_harvest_date from planted_date + DTM
  - Import can skip if already in CSV, or let save() fill them in
- **InventoryLedger.running_balance** auto-calculated from prior transactions
  - Tests whether import respects model save() logic

### 3. Date Formats
- All dates in **ISO format (YYYY-MM-DD)**
- Consistent across all CSVs
- No week numbers, no ambiguous MM/DD vs. DD/MM

### 4. Null/Missing Values
- Some cells genuinely empty (planned-but-not-yet-executed actuals)
- Some cells "na" or blank (e.g., spacing for direct-seeded crops)
- Harvest bins sometimes 0 (continuous harvest items like basil)

### 5. Decimal/Money Formats
- Prices use standard decimal notation: 8.50, 3.99
- No $ prefix or comma formatting
- Quantities with .1 or .2 decimal places (e.g., 5.2 lbs basil)

### 6. Array Fields (PostgreSQL)
- `SalesChannel.days_of_week` uses "+" separator in CSV
  - Example: "Monday + Wednesday" → must parse to ["Monday", "Wednesday"]
  - "Saturday" → ["Saturday"]

### 7. Unique Constraints
- 2023 has 12 plantings (no duplicates by crop/block/date combo)
- 2024 has 10 plantings (different plantings, no collision with 2023)
- CropInfo names are unique across both years (no "Tomato" vs "Tomato Beefsteak" collision)

### 8. Choice Fields (Django CharField with choices)
- `Block.block_type`: "Field", "High Tunnel", "Greenhouse" (capitalized in CSV)
  - Must map to model values: "field", "high_tunnel", "greenhouse"
- `CropInfo.fresh_or_storage`: "Fresh" or "Storage"
  - Must map to model: "fresh" or "storage"
- `Planting.status`: "planned", "complete", etc. (lowercase in data)
- `FieldWalkNote.condition`: "good", "fair", "poor", "failed"
- `HarvestEvent.quality_grade`: "prime", "seconds", "mixed"

---

## Usage for Import Testing

### Dry-Run (Validate without saving)
```bash
python manage.py import_historical_data data/sample_import --dry-run --verbose
```

### Full Import
```bash
python manage.py import_historical_data data/sample_import
```

### Specific Year
```bash
python manage.py import_historical_data data/sample_import --start-year 2023 --end-year 2023
```

---

## Testing Checkpoints

After import, verify:

1. **Reference Data**
   - 4 blocks created
   - 12 crops with all fields populated
   - 20 crop-by-season profiles
   - 6 sales channels
   - 20 crop sales formats

2. **2023 Plantings**
   - 12 plantings created with planned + actual data
   - Harvest events generated: should have 14-16 events per multi-week planting
   - Nursery events: 18 total across seeding, pot-up, transplant phases
   - Field walk notes: 29 observations recorded
   - Inventory ledger: ~180 transactions with running balances calculated

3. **2024 Plantings**
   - 10 plantings created (planned only)
   - Status correctly set to "planned"
   - Actual fields null

4. **Sales & Inventory**
   - Sales events linked to products
   - Running balance calculated correctly in inventory ledger
   - No orphaned foreign keys

---

## Sample Data Authorship Notes

- Data is **realistic but fictional**
- Inspired by actual small-farm operations
- Includes intentional variations:
  - Some yields exceed plan (peppers +10%)
  - Some yields below plan (carrots -5%, cucumbers -12%)
  - Quality grades decline in late season
  - Heat-related crop losses (lettuce bolting)
  - Labor efficiency variations
  - Spoilage and waste entries
- **Suitable for:**
  - Testing import workflows
  - Demo/presentation data
  - Documentation examples
  - Load testing (scales to ~5000 harvest events per year)

---

## Next Steps for Full Import Workflow

1. **Build `import_historical_data.py`** command using patterns from existing `import_reference_data.py`
2. **Run dry-run** to validate all FK resolutions and constraints
3. **Execute full import** and verify database state
4. **Test queries:** Access data via ORM to ensure relationships work
5. **Generate export** to verify round-trip integrity
6. **Document CSV schema** for users exporting real data
