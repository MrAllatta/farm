"""core/management/commands/import_historical_data.py

Complete one-time import command for 5 years of historical farm data.
Imports reference data, planning years, plantings, operations, and sales records
in a dependency-aware order (5 tiers).

Usage:
    python manage.py import_historical_data /path/to/data/dir
    python manage.py import_historical_data /path/to/data/dir --start-year 2021 --end-year 2025
    python manage.py import_historical_data /path/to/data/dir --dry-run
"""

import csv
import os
import sys
from decimal import Decimal, InvalidOperation
from datetime import datetime
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from reference.models import CropInfo, Block, CropBySeason, CropSalesFormat, SalesChannel
from planning.models import PlanningYear, Planting, NurseryEvent, HarvestEvent
from operations.models import FieldWalkNote, InventoryLedger, PackAllocation
from sales.models import SalesEvent, QuickSalesEntry
from core.models import RotationHistory


class Command(BaseCommand):
    help = """Import 5 years of historical farm data from CSV files.
    
    Imports reference data, planning years, plantings, operations, and sales in
    dependency-aware order. Handles FK resolution by name, calculated fields,
    choice field mapping, and running balance sequencing."""

    def add_arguments(self, parser):
        parser.add_argument(
            "data_dir",
            type=str,
            help="Directory containing CSV subdirectories (reference data at root, years in subdirs)",
        )
        parser.add_argument(
            "--start-year",
            type=int,
            default=2021,
            help="First year to import (default: 2021)",
        )
        parser.add_argument(
            "--end-year",
            type=int,
            default=2025,
            help="Last year to import (default: 2025)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate without saving any data",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Detailed per-row output",
        )

    def handle(self, *args, **options):
        self.data_dir = options["data_dir"]
        self.dry_run = options["dry_run"]
        self.verbose = options["verbose"]
        self.start_year = options["start_year"]
        self.end_year = options["end_year"]

        # Track statistics
        self.stats = defaultdict(lambda: {"processed": 0, "created": 0, "skipped": 0, "errors": 0})

        # Cache for FK lookups
        self.crop_cache = {}
        self.block_cache = {}
        self.channel_cache = {}
        self.product_cache = {}
        self.planning_year_cache = {}
        self.planting_cache = {}
        self.harvest_event_cache = {}

        if not os.path.isdir(self.data_dir):
            raise CommandError(f"Data directory not found: {self.data_dir}")

        if self.dry_run:
            self.stdout.write(self.style.WARNING("\n⚠️  DRY RUN — no data will be saved\n"))

        try:
            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self.stdout.write("TIER 1: Reference Data (Independent)\n")
            self.stdout.write("=" * 70)
            self._import_reference_tier()

            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self.stdout.write("TIER 2: Planning Years & Plantings\n")
            self.stdout.write("=" * 70)
            self._import_years_and_plantings()

            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self.stdout.write("TIER 3: Nursery & Harvest Events\n")
            self.stdout.write("=" * 70)
            self._import_nursery_and_harvest()

            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self.stdout.write("TIER 4: Operations (Field Walks, Inventory, Packing)\n")
            self.stdout.write("=" * 70)
            self._import_operations_tier()

            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self.stdout.write("TIER 5: Sales & Rotation History\n")
            self.stdout.write("=" * 70)
            self._import_sales_and_rotation()

            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self._print_summary()
            self.stdout.write("=" * 70 + "\n")

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"\n❌ FATAL ERROR: {e}"))
            if self.verbose:
                import traceback
                traceback.print_exc()
            sys.exit(1)

    # ============================================================================
    # TIER 1: Reference Data (Independent)
    # ============================================================================

    def _import_reference_tier(self):
        """Import reference data: blocks, crops, crop_by_season, channels, products."""
        self._import_blocks()
        self._import_crops()
        self._import_crop_by_season()
        self._import_sales_channels()
        self._import_crop_sales_formats()

    def _import_blocks(self):
        """Import block definitions."""
        path = os.path.join(self.data_dir, "blocks.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ blocks.csv not found\n")
            return

        self.stdout.write("Importing blocks...")

        type_map = {
            "Field": "field",
            "High Tunnel": "high_tunnel",
            "Greenhouse": "greenhouse",
        }

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    name = row["Block"].strip()
                    if not name:
                        continue

                    block_type = type_map.get(row["Block Type"].strip(), "field")

                    data = {
                        "block_type": block_type,
                        "num_beds": self._int(row["# of Beds"]),
                        "bed_width_feet": self._dec(row["Bed Width (feet)"] or "0"),
                        "bedfeet_per_bed": self._int(row["Bedfeet per Bed"] or 0),
                    }

                    if not self.dry_run:
                        obj, created = Block.objects.update_or_create(name=name, defaults=data)
                        self.stats["Block"]["created" if created else "processed"] += 1
                        self.block_cache[name] = obj
                    else:
                        self.block_cache[name] = name
                        self.stats["Block"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["Block"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['Block']['processed']} processed, "
            f"{self.stats['Block']['errors']} errors\n"
        )

    def _import_crops(self):
        """Import crop info."""
        path = os.path.join(self.data_dir, "crop_info.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ crop_info.csv not found\n")
            return

        self.stdout.write("Importing crop info...")

        seen_names = set()

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    name = row["Crop"].strip()
                    if not name or name in seen_names:
                        if name in seen_names:
                            self.stats["CropInfo"]["skipped"] += 1
                        continue

                    seen_names.add(name)

                    crop_type = row.get("Type", "").strip() or "Vegetables"
                    botanical_family = row.get("Botanical Family", "").strip() or ""

                    # Determine propagation type
                    propagation_type = "seed"
                    if name.startswith("Garlic"):
                        propagation_type = "vegetative_clove"
                    elif name.startswith("Potato") or name == "Sweet Potatoes":
                        propagation_type = "vegetative_tuber"

                    is_perennial = name in ("Asparagus",)

                    fresh_or_storage = row.get("Fresh or Storage", "Fresh").strip().lower()
                    if fresh_or_storage not in ("fresh", "storage"):
                        fresh_or_storage = "fresh"

                    data = {
                        "crop_type": crop_type,
                        "botanical_family": botanical_family,
                        "propagation_type": propagation_type,
                        "is_perennial": is_perennial,
                        "fresh_or_storage": fresh_or_storage,
                        "storage_weeks": self._int(row.get("Storage Weeks", 0)),
                        "harvest_unit": row.get("Harvest Units", "pounds").strip() or "pounds",
                        "avg_unit_weight": self._dec(row.get("Average Unit Weight", 1)),
                        "units_per_bin": self._int_or_none(row.get("Units Per Bin")),
                        "harvest_bin": row.get("Harvest Bin", "").strip(),
                        "harvest_tools": row.get("Harvest Tools", "").strip(),
                        "harvest_rate_per_hour": self._int_or_none(
                            row.get("Harvest Rate (units per hour)")
                        ),
                        "nursery_weeks": self._int(row.get("Nursery Weeks", 0)),
                        "weeks_until_pot_up": self._int(row.get("Weeks Until Pot Up", 0)),
                        "pot_up_tray_size": self._int_or_none(row.get("Pot Up Tray Size")),
                        "seeded_tray_size": self._int_or_none(row.get("Seeded Tray Size")),
                        "seeds_per_cell": self._int(row.get("Seeds Per Cell", 1)) or 1,
                        "thinned_plants": self._int(row.get("Thinned Plants", 0)),
                        "seeds_per_ounce": self._dec_or_none(row.get("Seeds Per Ounce")),
                    }

                    if not self.dry_run:
                        obj, created = CropInfo.objects.update_or_create(name=name, defaults=data)
                        self.stats["CropInfo"]["created" if created else "processed"] += 1
                        self.crop_cache[name] = obj
                    else:
                        self.crop_cache[name] = name
                        self.stats["CropInfo"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["CropInfo"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['CropInfo']['processed']} processed, "
            f"{self.stats['CropInfo']['skipped']} skipped, "
            f"{self.stats['CropInfo']['errors']} errors\n"
        )

    def _import_crop_by_season(self):
        """Import crop-by-season profiles."""
        path = os.path.join(self.data_dir, "crop_by_season.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ crop_by_season.csv not found\n")
            return

        self.stdout.write("Importing crop by season...")

        type_map = {
            "Field": "field",
            "High Tunnel": "high_tunnel",
            "Greenhouse": "greenhouse",
        }

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    crop_name = row["Crop"].strip()
                    block_type_raw = row["Block Type"].strip()

                    if not crop_name:
                        continue

                    block_type = type_map.get(block_type_raw)
                    if not block_type:
                        self.stats["CropBySeason"]["errors"] += 1
                        continue

                    crop = self._get_crop(crop_name)
                    if not crop:
                        self.stderr.write(f"    ERROR row {i}: crop not found '{crop_name}'")
                        self.stats["CropBySeason"]["errors"] += 1
                        continue

                    # Parse spacing values
                    tp_spacing_raw = row.get("TP Inrow Spacing (ft)", "").strip()
                    tp_spacing = None
                    if tp_spacing_raw and tp_spacing_raw.lower() != "na":
                        try:
                            tp_spacing = Decimal(tp_spacing_raw)
                        except InvalidOperation:
                            pass

                    ds_rate_raw = row.get("DS Seed Rate (seeds/ rowfoot)", "").strip()
                    ds_rate = None
                    if ds_rate_raw and ds_rate_raw.lower() not in ("na", ""):
                        try:
                            ds_rate = int(float(ds_rate_raw))
                        except ValueError:
                            pass

                    dtm = self._int(row.get("DTM Days To Maturity", 0))
                    if not dtm:
                        self.stats["CropBySeason"]["skipped"] += 1
                        continue

                    data = {
                        "field_week_start": self._int(row.get("Field Week Start", 1)),
                        "field_week_end": self._int(row.get("Field Week End", 52)),
                        "total_yield_per_bedfoot": self._dec(row.get("Total Yield Per Bedfoot", 0)),
                        "harvest_weeks": self._int(row.get("Harvest Weeks", 1)) or 1,
                        "dtm_days": dtm,
                        "rows_per_bed": self._int(row.get("Rows Per Bed", 1)) or 1,
                        "ds_seed_rate": ds_rate,
                        "tp_inrow_spacing": tp_spacing,
                        "seeder_settings": row.get("Seeder Settings", "").strip(),
                        "trellis_system": row.get("Trellis System", "").strip(),
                        "mulch": row.get("Mulch", "").strip(),
                        "row_cover": row.get("Row Cover", "").strip(),
                        "irrigation": row.get("Irrigation", "").strip(),
                    }

                    if not self.dry_run:
                        obj, created = CropBySeason.objects.update_or_create(
                            crop=crop, block_type=block_type, defaults=data
                        )
                        self.stats["CropBySeason"]["created" if created else "processed"] += 1
                    else:
                        self.stats["CropBySeason"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["CropBySeason"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['CropBySeason']['processed']} processed, "
            f"{self.stats['CropBySeason']['skipped']} skipped, "
            f"{self.stats['CropBySeason']['errors']} errors\n"
        )

    def _import_sales_channels(self):
        """Import sales channels."""
        path = os.path.join(self.data_dir, "sales_channels.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ sales_channels.csv not found\n")
            return

        self.stdout.write("Importing sales channels...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    name = row["Channel Name"].strip()
                    if not name:
                        continue

                    # Parse days: "Monday + Wednesday" → ["Monday", "Wednesday"]
                    days_raw = row.get("Days of the Week", "").strip()
                    days = [d.strip() for d in days_raw.replace("+", ",").split(",") if d.strip()]

                    target_raw = row.get("$ Target per week", "0")
                    target = self._dec(target_raw.replace("$", "").replace(",", ""))

                    is_csa = row.get("is_csa", "false").strip().lower() == "true"

                    data = {
                        "days_of_week": days,
                        "start_week": self._int(row.get("Start Week Num", 1)),
                        "end_week": self._int(row.get("End Week Num", 52)),
                        "weekly_target": target,
                        "is_csa": is_csa,
                        "allocation_priority": self._int(row.get("Priority", i), i),
                    }

                    if not self.dry_run:
                        obj, created = SalesChannel.objects.update_or_create(
                            name=name, defaults=data
                        )
                        self.stats["SalesChannel"]["created" if created else "processed"] += 1
                        self.channel_cache[name] = obj
                    else:
                        self.channel_cache[name] = name
                        self.stats["SalesChannel"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["SalesChannel"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['SalesChannel']['processed']} processed, "
            f"{self.stats['SalesChannel']['errors']} errors\n"
        )

    def _import_crop_sales_formats(self):
        """Import crop sales formats (products)."""
        path = os.path.join(self.data_dir, "crop_sales_formats.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ crop_sales_formats.csv not found\n")
            return

        self.stdout.write("Importing crop sales formats...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    # Handle both "Crop" and "Crop Name" headers
                    crop_name = row.get("Crop Name") or row.get("Crop")
                    if crop_name:
                        crop_name = crop_name.strip()
                    product_name = row.get("Product Name", "").strip()

                    if not crop_name or not product_name:
                        continue

                    crop = self._get_crop(crop_name)
                    if not crop:
                        self.stderr.write(f"    ERROR row {i}: crop not found '{crop_name}'")
                        self.stats["CropSalesFormat"]["errors"] += 1
                        continue

                    data = {
                        "sale_price": self._dec(row.get("Sale Price", 0)),
                        "sale_unit": row.get("Sale Unit", "").strip() or "pound",
                        "harvest_qty_per_sale_unit": self._dec(
                            row.get("Harvest Qty Per Sale Unit", 1)
                        ),
                        "sku": row.get("SKU", "").strip(),
                        "is_active": row.get("Is Active", "true").strip().lower() == "true",
                    }

                    if not self.dry_run:
                        obj, created = CropSalesFormat.objects.update_or_create(
                            crop=crop, product_name=product_name, defaults=data
                        )
                        self.stats["CropSalesFormat"]["created" if created else "processed"] += 1
                        self.product_cache[(crop_name, product_name)] = obj
                    else:
                        self.product_cache[(crop_name, product_name)] = (crop_name, product_name)
                        self.stats["CropSalesFormat"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["CropSalesFormat"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['CropSalesFormat']['processed']} processed, "
            f"{self.stats['CropSalesFormat']['errors']} errors\n"
        )

    # ============================================================================
    # TIER 2: Planning Years & Plantings
    # ============================================================================

    def _import_years_and_plantings(self):
        """Import planning years and plantings."""
        for year in range(self.start_year, self.end_year + 1):
            year_dir = os.path.join(self.data_dir, f"year_{year}")
            if not os.path.isdir(year_dir):
                continue

            self._import_planning_year(year, year_dir)
            self._import_plantings(year, year_dir)

    def _import_planning_year(self, year, year_dir):
        """Import planning year record."""
        path = os.path.join(year_dir, "planning_year.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing planning year {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    py_year = self._int(row["Year"])
                    status = row.get("Status", "planning").strip().lower()
                    if status not in ("planning", "active", "complete", "archived"):
                        status = "planning"

                    overplant = self._dec(row.get("Overplant Factor", "1.10"))

                    data = {
                        "status": status,
                        "overplant_factor": overplant,
                    }

                    if not self.dry_run:
                        obj, created = PlanningYear.objects.update_or_create(
                            year=py_year, defaults=data
                        )
                        self.stats["PlanningYear"]["created" if created else "processed"] += 1
                        self.planning_year_cache[py_year] = obj
                    else:
                        self.planning_year_cache[py_year] = py_year
                        self.stats["PlanningYear"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["PlanningYear"]["errors"] += 1

    def _import_plantings(self, year, year_dir):
        """Import plantings."""
        path = os.path.join(year_dir, "plantings.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ year_{year}/plantings.csv not found\n")
            return

        self.stdout.write(f"Importing plantings {year}...")

        status_map = {
            "Planned": "planned",
            "Seeded": "seeded",
            "Planted": "planted",
            "Growing": "growing",
            "Harvesting": "harvesting",
            "Complete": "complete",
            "Failed": "failed",
            "Skipped": "skipped",
            "Revised": "revised",
        }

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    planting_id = row.get("ID", "").strip()
                    # Handle both "Crop Name" and "Crop" headers
                    crop_name = row.get("Crop Name") or row.get("Crop")
                    if crop_name:
                        crop_name = crop_name.strip()
                    block_name = row.get("Block Name") or row.get("Block")
                    if block_name:
                        block_name = block_name.strip()
                    # Block Type may not be needed for lookup since we use block.block_type
                    # block_type = row.get("Block Type", "").strip()

                    if not crop_name or not block_name:
                        continue

                    # Get FKs
                    planning_year = self._get_planning_year(year)
                    crop = self._get_crop(crop_name)
                    block = self._get_block(block_name)

                    if not planning_year or not crop or not block:
                        self.stderr.write(
                            f"    ERROR row {i}: missing FK (PY={planning_year}, crop={crop}, block={block})"
                        )
                        self.stats["Planting"]["errors"] += 1
                        continue

                    # Get crop_season — skip in dry-run (no DB objects)
                    if not self.dry_run:
                        try:
                            crop_season = CropBySeason.objects.get(
                                crop=crop, block_type=block.block_type
                            )
                        except CropBySeason.DoesNotExist:
                            self.stderr.write(
                                f"    ERROR row {i}: no crop_season for {crop_name}/{block.block_type}"
                            )
                            self.stats["Planting"]["errors"] += 1
                            continue

                    # Parse dates
                    plant_date_str = row.get("Planned Plant Date", "").strip()
                    if not plant_date_str:
                        self.stats["Planting"]["skipped"] += 1
                        continue

                    plant_date = self._parse_date(plant_date_str)

                    data = {
                        "variety": row.get("Variety", "").strip(),
                        "bed_start": self._int(row.get("Bed Start", 1)),
                        "bed_end": self._int(row.get("Bed End", 1)),
                        "planned_bedfeet": self._int(row.get("Planned Bedfeet", 100)),
                        "planned_plant_date": plant_date,
                        # Calculated fields — let model.save() handle them
                        "status": status_map.get(row.get("Status", "planned").strip(), "planned"),
                        "notes": row.get("Notes", "").strip(),
                    }

                    # Actual fields (may be null for planned-only years)
                    actual_plant_date_str = row.get("Actual Plant Date", "").strip()
                    if actual_plant_date_str:
                        data["actual_plant_date"] = self._parse_date(actual_plant_date_str)
                        data["actual_bedfeet"] = self._int_or_none(row.get("Actual Bedfeet"))

                    actual_harvest_str = row.get("Actual Total Yield", "").strip()
                    if actual_harvest_str:
                        data["actual_total_yield"] = self._dec_or_none(actual_harvest_str)

                    if not self.dry_run:
                        obj, created = Planting.objects.update_or_create(
                            planning_year=planning_year,
                            crop=crop,
                            block=block,
                            bed_start=data["bed_start"],
                            bed_end=data["bed_end"],
                            planned_plant_date=plant_date,
                            defaults=data,
                        )
                        self.stats["Planting"]["created" if created else "processed"] += 1
                        # Cache by planting_id for later lookups
                        if planting_id:
                            self.planting_cache[planting_id] = obj
                    else:
                        self.stats["Planting"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["Planting"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['Planting']['processed']} processed, "
            f"{self.stats['Planting']['skipped']} skipped, "
            f"{self.stats['Planting']['errors']} errors\n"
        )

    # ============================================================================
    # TIER 3: Nursery & Harvest Events
    # ============================================================================

    def _import_nursery_and_harvest(self):
        """Import nursery and harvest events."""
        for year in range(self.start_year, self.end_year + 1):
            year_dir = os.path.join(self.data_dir, f"year_{year}")
            if not os.path.isdir(year_dir):
                continue

            self._import_nursery_events(year, year_dir)
            self._import_harvest_events(year, year_dir)

    def _import_nursery_events(self, year, year_dir):
        """Import nursery events."""
        path = os.path.join(year_dir, "nursery_events.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing nursery events {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    planting_id = row.get("Planting ID", "").strip()
                    planting = self._get_planting(planting_id)

                    if not planting:
                        self.stats["NurseryEvent"]["skipped"] += 1
                        continue

                    event_type = row.get("Event Type", "").strip().lower()
                    if event_type not in ("seed", "pot_up", "harden", "transplant"):
                        event_type = "seed"

                    planned_date_str = row.get("Planned Date", "").strip()
                    if not planned_date_str:
                        self.stats["NurseryEvent"]["skipped"] += 1
                        continue

                    data = {
                        "event_type": event_type,
                        "planned_date": self._parse_date(planned_date_str),
                        "planned_tray_count": self._int_or_none(row.get("Planned Tray Count")),
                        "planned_tray_size": self._int_or_none(row.get("Planned Tray Size")),
                    }

                    # Actual fields
                    actual_date_str = row.get("Actual Date", "").strip()
                    if actual_date_str:
                        data["actual_date"] = self._parse_date(actual_date_str)
                        data["actual_tray_count"] = self._int_or_none(row.get("Actual Tray Count"))
                        data["actual_tray_size"] = self._int_or_none(row.get("Actual Tray Size"))
                        data["actual_germination_rate"] = self._dec_or_none(
                            row.get("Actual Germination Rate")
                        )

                    data["notes"] = row.get("Notes", "").strip()

                    if not self.dry_run:
                        obj, created = NurseryEvent.objects.update_or_create(
                            planting=planting,
                            planned_date=data["planned_date"],
                            event_type=event_type,
                            defaults=data,
                        )
                        self.stats["NurseryEvent"]["created" if created else "processed"] += 1
                    else:
                        self.stats["NurseryEvent"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["NurseryEvent"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['NurseryEvent']['processed']} processed, "
            f"{self.stats['NurseryEvent']['skipped']} skipped, "
            f"{self.stats['NurseryEvent']['errors']} errors\n"
        )

    def _import_harvest_events(self, year, year_dir):
        """Import harvest events."""
        path = os.path.join(year_dir, "harvest_events.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing harvest events {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    planting_id = row.get("Planting ID", "").strip()
                    planting = self._get_planting(planting_id)

                    if not planting:
                        self.stats["HarvestEvent"]["skipped"] += 1
                        continue

                    planned_date_str = row.get("Planned Date", "").strip()
                    if not planned_date_str:
                        self.stats["HarvestEvent"]["skipped"] += 1
                        continue

                    planned_date = self._parse_date(planned_date_str)

                    data = {
                        "planned_date": planned_date,
                        "planned_quantity": self._dec(row.get("Planned Quantity", 0)),
                        "planned_units": row.get("Planned Units", "pounds").strip(),
                    }

                    # Actual fields
                    actual_date_str = row.get("Actual Date", "").strip()
                    if actual_date_str:
                        data["actual_date"] = self._parse_date(actual_date_str)
                        data["actual_quantity"] = self._dec_or_none(row.get("Actual Quantity"))
                        data["actual_units"] = row.get("Actual Units", "").strip()
                        data["actual_bins"] = self._dec_or_none(row.get("Actual Bins"))
                        data["actual_bin_type"] = row.get("Actual Bin Type", "").strip()
                        data["actual_hours"] = self._dec_or_none(row.get("Actual Hours"))
                        data["actual_workers"] = self._int_or_none(row.get("Actual Workers"))

                    quality = row.get("Quality Grade", "").strip().lower()
                    if quality not in ("prime", "seconds", "mixed"):
                        quality = ""
                    data["quality_grade"] = quality

                    data["notes"] = row.get("Notes", "").strip()

                    if not self.dry_run:
                        obj, created = HarvestEvent.objects.update_or_create(
                            planting=planting,
                            planned_date=planned_date,
                            defaults=data,
                        )
                        self.stats["HarvestEvent"]["created" if created else "processed"] += 1
                        # Cache harvest events for inventory lookups
                        cache_key = (planting_id, str(planned_date))
                        self.harvest_event_cache[cache_key] = obj
                    else:
                        self.stats["HarvestEvent"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["HarvestEvent"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['HarvestEvent']['processed']} processed, "
            f"{self.stats['HarvestEvent']['skipped']} skipped, "
            f"{self.stats['HarvestEvent']['errors']} errors\n"
        )

    # ============================================================================
    # TIER 4: Operations (Field Walks, Inventory, Packing)
    # ============================================================================

    def _import_operations_tier(self):
        """Import operations: field walk notes, inventory ledger, pack allocations."""
        for year in range(self.start_year, self.end_year + 1):
            year_dir = os.path.join(self.data_dir, f"year_{year}")
            if not os.path.isdir(year_dir):
                continue

            self._import_field_walk_notes(year, year_dir)
            self._import_inventory_ledger(year, year_dir)
            self._import_pack_allocations(year, year_dir)

    def _import_field_walk_notes(self, year, year_dir):
        """Import field walk notes."""
        path = os.path.join(year_dir, "field_walk_notes.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing field walk notes {year}...")

        condition_map = {
            "Good": "good",
            "Fair": "fair",
            "Poor": "poor",
            "Failed": "failed",
        }

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    planting_id = row.get("Planting ID", "").strip()
                    planting = self._get_planting(planting_id)

                    if not planting:
                        self.stats["FieldWalkNote"]["skipped"] += 1
                        continue

                    walk_date_str = row.get("Walk Date", "").strip()
                    if not walk_date_str:
                        self.stats["FieldWalkNote"]["skipped"] += 1
                        continue

                    condition_raw = row.get("Condition", "good").strip()
                    condition = condition_map.get(condition_raw, "good")

                    data = {
                        "walk_date": self._parse_date(walk_date_str),
                        "condition": condition,
                        "yield_adjust_pct": self._int(row.get("Yield Adjust %", 100), 100),
                        "notes": row.get("Notes", "").strip(),
                    }

                    # Optional adjusted dates
                    adj_first_str = row.get("Adjusted First Harvest Date", "").strip()
                    if adj_first_str:
                        data["adjusted_first_harvest_date"] = self._parse_date(adj_first_str)

                    adj_last_str = row.get("Adjusted Last Harvest Date", "").strip()
                    if adj_last_str:
                        data["adjusted_last_harvest_date"] = self._parse_date(adj_last_str)

                    if not self.dry_run:
                        obj, created = FieldWalkNote.objects.update_or_create(
                            planting=planting,
                            walk_date=data["walk_date"],
                            defaults=data,
                        )
                        self.stats["FieldWalkNote"]["created" if created else "processed"] += 1
                    else:
                        self.stats["FieldWalkNote"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["FieldWalkNote"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['FieldWalkNote']['processed']} processed, "
            f"{self.stats['FieldWalkNote']['skipped']} skipped, "
            f"{self.stats['FieldWalkNote']['errors']} errors\n"
        )

    def _import_inventory_ledger(self, year, year_dir):
        """Import inventory ledger entries."""
        path = os.path.join(year_dir, "inventory_ledger.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing inventory ledger {year}...")

        event_type_map = {
            "Harvest In": "harvest_in",
            "Sale Out": "sale_out",
            "Return In": "return_in",
            "Waste Out": "waste_out",
            "Transfer": "transfer",
            "Quality Check": "quality_check",
            "Year End Count": "year_end_count",
            "Adjustment": "adjustment",
        }

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    # CSV has "Crop Name" not "Crop"
                    crop_name = row.get("Crop Name", "").strip()
                    crop = self._get_crop(crop_name)

                    if not crop:
                        self.stats["InventoryLedger"]["skipped"] += 1
                        continue

                    event_date_str = row.get("Event Date", "").strip()
                    if not event_date_str:
                        self.stats["InventoryLedger"]["skipped"] += 1
                        continue

                    event_type_raw = row.get("Event Type", "adjustment").strip()
                    event_type = event_type_map.get(event_type_raw, "adjustment")

                    data = {
                        "crop": crop,
                        "event_date": self._parse_date(event_date_str),
                        "event_type": event_type,
                        "quantity": self._dec(row.get("Quantity", 0)),
                        "expiry_date": None,
                        "storage_location": row.get("Storage Location", "").strip(),
                        "notes": row.get("Notes", "").strip(),
                    }

                    # Optional FK to harvest event
                    planting_id = row.get("Planting ID", "").strip()
                    harvest_date_str = row.get("Harvest Date", "").strip()
                    if planting_id and harvest_date_str:
                        cache_key = (planting_id, harvest_date_str)
                        he = self.harvest_event_cache.get(cache_key)
                        if he:
                            data["harvest_event"] = he

                    # Optional expiry
                    expiry_str = row.get("Expiry Date", "").strip()
                    if expiry_str:
                        data["expiry_date"] = self._parse_date(expiry_str)

                    if not self.dry_run:
                        obj, created = InventoryLedger.objects.update_or_create(
                            crop=crop,
                            event_date=data["event_date"],
                            event_type=event_type,
                            quantity=data["quantity"],
                            defaults=data,
                        )
                        self.stats["InventoryLedger"]["created" if created else "processed"] += 1
                    else:
                        self.stats["InventoryLedger"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["InventoryLedger"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['InventoryLedger']['processed']} processed, "
            f"{self.stats['InventoryLedger']['skipped']} skipped, "
            f"{self.stats['InventoryLedger']['errors']} errors\n"
        )

    def _import_pack_allocations(self, year, year_dir):
        """Import pack allocations."""
        path = os.path.join(year_dir, "pack_allocations.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing pack allocations {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    planting_id = row.get("Planting ID", "").strip()
                    harvest_date_str = row.get("Harvest Date", "").strip()
                    channel_name = row.get("Channel", "").strip()
                    product_name = row.get("Product", "").strip()
                    pack_date_str = row.get("Pack Date", "").strip()

                    if not (channel_name and product_name and pack_date_str):
                        self.stats["PackAllocation"]["skipped"] += 1
                        continue

                    # Get FKs
                    channel = self._get_channel(channel_name)
                    product = self._get_product_by_name(product_name)

                    if not (channel and product):
                        self.stats["PackAllocation"]["skipped"] += 1
                        continue

                    data = {
                        "channel": channel,
                        "product": product,
                        "pack_date": self._parse_date(pack_date_str),
                        "quantity": self._dec(row.get("Quantity", 0)),
                        "notes": row.get("Notes", "").strip(),
                    }

                    # Optional FKs
                    if planting_id and harvest_date_str:
                        cache_key = (planting_id, harvest_date_str)
                        he = self.harvest_event_cache.get(cache_key)
                        if he:
                            data["harvest_event"] = he

                    if not self.dry_run:
                        obj, created = PackAllocation.objects.update_or_create(
                            channel=channel,
                            product=product,
                            pack_date=data["pack_date"],
                            defaults=data,
                        )
                        self.stats["PackAllocation"]["created" if created else "processed"] += 1
                    else:
                        self.stats["PackAllocation"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["PackAllocation"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['PackAllocation']['processed']} processed, "
            f"{self.stats['PackAllocation']['skipped']} skipped, "
            f"{self.stats['PackAllocation']['errors']} errors\n"
        )

    # ============================================================================
    # TIER 5: Sales & Rotation History
    # ============================================================================

    def _import_sales_and_rotation(self):
        """Import sales events, quick entries, and rotation history."""
        for year in range(self.start_year, self.end_year + 1):
            year_dir = os.path.join(self.data_dir, f"year_{year}")
            if not os.path.isdir(year_dir):
                continue

            self._import_sales_events(year, year_dir)
            self._import_quick_sales_entries(year, year_dir)

        self._import_rotation_history()

    def _import_sales_events(self, year, year_dir):
        """Import sales events."""
        path = os.path.join(year_dir, "sales_events.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing sales events {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    # CSV has "Channel Name" not "Channel"
                    channel_name = (
                        row.get("Channel Name") or row.get("Channel") or ""
                    ).strip()
                    sale_date_str = row.get("Sale Date", "").strip()

                    if not (channel_name and sale_date_str):
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    channel = self._get_channel(channel_name)
                    if not channel:
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    data = {
                        "channel": channel,
                        "sale_date": self._parse_date(sale_date_str),
                    }

                    # CSV has "Product Name" not "Product"
                    product_name = (
                        row.get("Product Name") or row.get("Product") or ""
                    ).strip()
                    if product_name:
                        product = self._get_product_by_name(product_name)
                        if product:
                            data["product"] = product

                    # Planned fields
                    planned_qty = row.get("Planned Quantity", "").strip()
                    if planned_qty:
                        data["planned_quantity"] = self._dec_or_none(planned_qty)

                    planned_rev = row.get("Planned Revenue", "").strip()
                    if planned_rev:
                        data["planned_revenue"] = self._dec_or_none(planned_rev)

                    # Actual fields
                    actual_qty = row.get("Actual Quantity", "").strip()
                    if actual_qty:
                        data["actual_quantity"] = self._dec_or_none(actual_qty)

                    actual_rev = row.get("Actual Revenue", "").strip()
                    if actual_rev:
                        data["actual_revenue"] = self._dec_or_none(actual_rev)

                    actual_price = row.get("Actual Price", "").strip()
                    if actual_price:
                        data["actual_price"] = self._dec_or_none(actual_price)

                    # Brought/returned
                    data["brought_quantity"] = self._dec_or_none(row.get("Brought Quantity"))
                    data["returned_quantity"] = self._dec_or_none(row.get("Returned Quantity"))

                    data["notes"] = row.get("Notes", "").strip()

                    if not self.dry_run:
                        obj, created = SalesEvent.objects.update_or_create(
                            channel=channel,
                            sale_date=data["sale_date"],
                            product=data.get("product"),
                            defaults=data,
                        )
                        self.stats["SalesEvent"]["created" if created else "processed"] += 1
                    else:
                        self.stats["SalesEvent"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["SalesEvent"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['SalesEvent']['processed']} processed, "
            f"{self.stats['SalesEvent']['skipped']} skipped, "
            f"{self.stats['SalesEvent']['errors']} errors\n"
        )

    def _import_quick_sales_entries(self, year, year_dir):
        """Import quick sales entries."""
        path = os.path.join(year_dir, "quick_sales_entries.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing quick sales entries {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    # CSV has "Channel Name" not "Channel"
                    channel_name = (
                        row.get("Channel Name") or row.get("Channel") or ""
                    ).strip()
                    sale_date_str = row.get("Sale Date", "").strip()

                    if not (channel_name and sale_date_str):
                        self.stats["QuickSalesEntry"]["skipped"] += 1
                        continue

                    channel = self._get_channel(channel_name)
                    if not channel:
                        self.stats["QuickSalesEntry"]["skipped"] += 1
                        continue

                    data = {
                        "channel": channel,
                        "sale_date": self._parse_date(sale_date_str),
                        "total_cash": self._dec(row.get("Total Cash", 0)),
                        "total_card": self._dec(row.get("Total Card", 0)),
                        "notes": row.get("Notes", "").strip(),
                    }

                    if not self.dry_run:
                        obj, created = QuickSalesEntry.objects.update_or_create(
                            channel=channel,
                            sale_date=data["sale_date"],
                            defaults=data,
                        )
                        self.stats["QuickSalesEntry"]["created" if created else "processed"] += 1
                    else:
                        self.stats["QuickSalesEntry"]["processed"] += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["QuickSalesEntry"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['QuickSalesEntry']['processed']} processed, "
            f"{self.stats['QuickSalesEntry']['skipped']} skipped, "
            f"{self.stats['QuickSalesEntry']['errors']} errors\n"
        )

    def _import_rotation_history(self):
        """Import rotation history from plantings."""
        self.stdout.write("Generating rotation history from plantings...")

        # Build rotation history from all plantings
        rotation_map = {}  # (block_id, year) -> botanical_family

        for planting in Planting.objects.select_related("block", "crop", "planning_year"):
            key = (planting.block_id, planting.planning_year.year)
            family = planting.crop.botanical_family

            if key not in rotation_map:
                rotation_map[key] = family
            else:
                # Multiple crops per block per year — use primary one
                pass

        # Create rotation history records
        if not self.dry_run:
            for (block_id, year), family in rotation_map.items():
                if family:
                    RotationHistory.objects.update_or_create(
                        block_id=block_id,
                        year=year,
                        defaults={"botanical_family": family},
                    )
                    self.stats["RotationHistory"]["created"] += 1
        else:
            self.stats["RotationHistory"]["processed"] = len(rotation_map)

        self.stdout.write(
            f" {self.stats['RotationHistory'].get('created', self.stats['RotationHistory'].get('processed', 0))} records generated\n"
        )

    # ============================================================================
    # Helper Methods
    # ============================================================================

    def _get_crop(self, crop_name):
        """Get or cache crop by name."""
        if crop_name not in self.crop_cache:
            try:
                if not self.dry_run:
                    self.crop_cache[crop_name] = CropInfo.objects.get(name=crop_name)
                else:
                    self.crop_cache[crop_name] = crop_name
            except CropInfo.DoesNotExist:
                self.crop_cache[crop_name] = None
        return self.crop_cache[crop_name]

    def _get_block(self, block_name):
        """Get or cache block by name."""
        if block_name not in self.block_cache:
            try:
                if not self.dry_run:
                    self.block_cache[block_name] = Block.objects.get(name=block_name)
                else:
                    self.block_cache[block_name] = block_name
            except Block.DoesNotExist:
                self.block_cache[block_name] = None
        return self.block_cache[block_name]

    def _get_channel(self, channel_name):
        """Get or cache sales channel by name."""
        if channel_name not in self.channel_cache:
            try:
                if not self.dry_run:
                    self.channel_cache[channel_name] = SalesChannel.objects.get(name=channel_name)
                else:
                    self.channel_cache[channel_name] = channel_name
            except SalesChannel.DoesNotExist:
                self.channel_cache[channel_name] = None
        return self.channel_cache[channel_name]

    def _get_product_by_name(self, product_name):
        """Get crop sales format by product name."""
        try:
            if not self.dry_run:
                return CropSalesFormat.objects.get(product_name=product_name)
            return product_name
        except CropSalesFormat.DoesNotExist:
            return None

    def _get_planning_year(self, year):
        """Get or cache planning year."""
        if year not in self.planning_year_cache:
            try:
                if not self.dry_run:
                    self.planning_year_cache[year] = PlanningYear.objects.get(year=year)
                else:
                    self.planning_year_cache[year] = year
            except PlanningYear.DoesNotExist:
                self.planning_year_cache[year] = None
        return self.planning_year_cache[year]

    def _get_planting(self, planting_id):
        """Get planting from cache."""
        return self.planting_cache.get(planting_id)

    def _parse_date(self, date_str):
        """Parse ISO date string YYYY-MM-DD."""
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid date format: {date_str}")

    # ============================================================================
    # Numeric Helpers (copied from import_reference_data.py)
    # ============================================================================

    def _int(self, value, default=0):
        """Parse integer, with default."""
        if not value:
            return default
        try:
            return int(float(str(value).strip()))
        except (ValueError, TypeError):
            return default

    def _int_or_none(self, value):
        """Parse integer or return None."""
        if not value or str(value).strip() in ("", "0", "na", "NA"):
            return None
        try:
            result = int(float(str(value).strip()))
            return result if result > 0 else None
        except (ValueError, TypeError):
            return None

    def _dec(self, value, default="0"):
        """Parse decimal, with default."""
        if not value:
            return Decimal(default)
        try:
            cleaned = str(value).strip().replace("$", "").replace(",", "")
            return Decimal(cleaned) if cleaned else Decimal(default)
        except (InvalidOperation, TypeError):
            return Decimal(default)

    def _dec_or_none(self, value):
        """Parse decimal or return None."""
        if not value or str(value).strip() in ("", "0", "na", "NA"):
            return None
        try:
            cleaned = str(value).strip().replace("$", "").replace(",", "")
            result = Decimal(cleaned)
            return result if result > 0 else None
        except (InvalidOperation, TypeError):
            return None

    def _print_summary(self):
        """Print summary statistics."""
        self.stdout.write("\n📊 SUMMARY\n")

        total_processed = 0
        total_created = 0
        total_skipped = 0
        total_errors = 0

        for model_name in sorted(self.stats.keys()):
            s = self.stats[model_name]
            processed = s.get("processed", 0)
            created = s.get("created", 0)
            skipped = s.get("skipped", 0)
            errors = s.get("errors", 0)

            total_processed += processed
            total_created += created
            total_skipped += skipped
            total_errors += errors

            status = "✓" if errors == 0 else "⚠"
            self.stdout.write(
                f"  {status} {model_name:25} processed={processed:3} created={created:3} "
                f"skipped={skipped:3} errors={errors:3}"
            )

        self.stdout.write(
            f"\n  TOTALS: processed={total_processed:3} created={total_created:3} "
            f"skipped={total_skipped:3} errors={total_errors:3}\n"
        )

        if self.dry_run:
            self.stdout.write(self.style.WARNING("\n⚠️  DRY RUN — no data saved"))
        else:
            self.stdout.write(self.style.SUCCESS("\n✓ All data saved successfully"))
