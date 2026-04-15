"""core/management/commands/import_reference_data.py"""

import csv
import os
from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError
from reference.models import CropInfo, CropBySeason, Block, SalesChannel


class Command(BaseCommand):
    help = "Import reference data from CSV files exported from org-mode tables"

    def add_arguments(self, parser):
        parser.add_argument("data_dir", type=str, help="Directory containing CSV files")
        parser.add_argument(
            "--dry-run", action="store_true", help="Parse and validate without saving"
        )

    def handle(self, *args, **options):
        data_dir = options["data_dir"]
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write("DRY RUN — no data will be saved\n")

        self._import_blocks(data_dir, dry_run)
        self._import_crops(data_dir, dry_run)
        self._import_crop_by_season(data_dir, dry_run)
        self._import_channels(data_dir, dry_run)

    def _import_blocks(self, data_dir, dry_run):
        path = os.path.join(data_dir, "blocks.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  Skipping blocks — {path} not found\n")
            return

        self.stdout.write("Importing blocks...\n")

        type_map = {
            "Field": "field",
            "High Tunnel": "high_tunnel",
            "Greenhouse": "greenhouse",
        }

        count = 0
        errors = 0

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    name = row["Block"].strip()
                    if not name:
                        continue

                    block_type = type_map.get(row["Block Type"].strip(), "field")

                    data = {
                        "block_type": block_type,
                        "num_beds": int(row["# of Beds"]),
                        "bed_width_feet": Decimal(row["Bed Width (feet)"] or "0"),
                        "bedfeet_per_bed": int(row["Bedfeet per Bed"] or 0),
                    }

                    if not dry_run:
                        Block.objects.update_or_create(name=name, defaults=data)

                    count += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"  Error on row {row}: {e}\n")
                    errors += 1

        self.stdout.write(f"  Blocks: {count} processed, {errors} errors\n")

    def _import_crops(self, data_dir, dry_run):
        path = os.path.join(data_dir, "crop_info.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  Skipping crops — {path} not found\n")
            return

        self.stdout.write("Importing crop info...\n")

        # Known data quality fixes
        type_fixes = {
            "Pepper Shishito": ("Peppers", None),  # was "Roots"
        }
        family_fixes = {
            "Lettuce Mix": ("Mix", None),  # was "Allium"
        }

        # Detect duplicates
        seen_names = set()
        count = 0
        errors = 0
        skipped = 0

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    name = row["Crop"].strip()
                    if not name:
                        continue

                    # Skip duplicates (keep first occurrence)
                    if name in seen_names:
                        self.stderr.write(f"  DUPLICATE: '{name}' — skipping second occurrence\n")
                        skipped += 1
                        continue
                    seen_names.add(name)

                    # Apply known fixes
                    crop_type = row.get("Type", "").strip()
                    botanical_family = row.get("Botanical Family", "").strip()

                    if name in type_fixes:
                        crop_type = type_fixes[name][0]
                        self.stdout.write(f"  FIX: {name} type → {crop_type}\n")
                    if name in family_fixes:
                        botanical_family = family_fixes[name][0]
                        self.stdout.write(f"  FIX: {name} family → {botanical_family}\n")

                    # Standardize harvest units
                    harvest_unit = row.get("Harvest Units", "").strip()
                    if harvest_unit == "eaches":
                        harvest_unit = "each"

                    # Determine propagation type
                    propagation_type = "seed"
                    if name.startswith("Garlic"):
                        propagation_type = "vegetative_clove"
                    elif name.startswith("Potato") or name == "Sweet Potatoes":
                        propagation_type = "vegetative_tuber"

                    # Determine perennial
                    is_perennial = name in ("Asparagus",)

                    # Parse fresh/storage
                    fresh_or_storage = row.get("Fresh or Storage", "Fresh").strip()
                    if fresh_or_storage not in ("Fresh", "Storage"):
                        fresh_or_storage = "Fresh"
                    fresh_or_storage = fresh_or_storage.lower()

                    data = {
                        "crop_type": crop_type,
                        "botanical_family": botanical_family,
                        "propagation_type": propagation_type,
                        "is_perennial": is_perennial,
                        "fresh_or_storage": fresh_or_storage,
                        "storage_weeks": self._int(row.get("Storage Weeks", 0)),
                        "harvest_unit": harvest_unit,
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

                    if not dry_run:
                        CropInfo.objects.update_or_create(name=name, defaults=data)

                    count += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(f"  Error on '{row.get('Crop', '?')}': {e}\n")
                    errors += 1

        self.stdout.write(
            f"  Crops: {count} imported, {skipped} duplicates skipped, " f"{errors} errors\n"
        )

    def _import_crop_by_season(self, data_dir, dry_run):
        path = os.path.join(data_dir, "crop_by_season.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  Skipping crop_by_season — {path} not found\n")
            return

        self.stdout.write("Importing crop by season...\n")

        type_map = {
            "Field": "field",
            "High Tunnel": "high_tunnel",
            "Greenhouse": "greenhouse",
        }

        count = 0
        errors = 0
        skipped = 0

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    crop_name = row["Crop"].strip()
                    block_type_raw = row["Block Type"].strip()

                    # Skip template rows
                    if not crop_name or crop_name == "choose crop":
                        skipped += 1
                        continue

                    block_type = type_map.get(block_type_raw)
                    if not block_type:
                        self.stderr.write(
                            f"  Unknown block type '{block_type_raw}' " f"for {crop_name}\n"
                        )
                        errors += 1
                        continue

                    # Find the crop
                    try:
                        crop = CropInfo.objects.get(name=crop_name)
                    except CropInfo.DoesNotExist:
                        self.stderr.write(
                            f"  Crop not found: '{crop_name}' — " f"skipping season profile\n"
                        )
                        errors += 1
                        continue

                    # Parse TP spacing — handle "na" values
                    tp_spacing_raw = row.get("TP Inrow Spacing (ft)", "").strip()
                    tp_spacing = None
                    if tp_spacing_raw and tp_spacing_raw.lower() != "na":
                        try:
                            tp_spacing = Decimal(tp_spacing_raw)
                        except InvalidOperation:
                            pass

                    # Parse DS seed rate
                    ds_rate_raw = row.get("DS Seed Rate (seeds/ rowfoot)", "").strip()
                    ds_rate = None
                    if ds_rate_raw and ds_rate_raw.lower() not in ("na", ""):
                        try:
                            ds_rate = int(float(ds_rate_raw))
                        except ValueError:
                            pass

                    # Validate DTM
                    dtm = self._int(row.get("DTM Days To Maturity", 0))
                    if not dtm:
                        self.stderr.write(
                            f"  WARNING: {crop_name}/{block_type_raw} " f"has no DTM — skipping\n"
                        )
                        skipped += 1
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

                    if not dry_run:
                        CropBySeason.objects.update_or_create(
                            crop=crop,
                            block_type=block_type,
                            defaults=data,
                        )

                    count += 1

                except (ValueError, KeyError, InvalidOperation) as e:
                    self.stderr.write(
                        f"  Error on '{row.get('Crop', '?')}' / "
                        f"'{row.get('Block Type', '?')}': {e}\n"
                    )
                    errors += 1

        self.stdout.write(
            f"  Crop by Season: {count} imported, {skipped} skipped, " f"{errors} errors\n"
        )

    def _import_channels(self, data_dir, dry_run):
        path = os.path.join(data_dir, "sales_channels.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  Skipping channels — {path} not found\n")
            return

        self.stdout.write("Importing sales channels...\n")

        count = 0
        errors = 0

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    name = row["Channel Name"].strip()
                    if not name:
                        continue

                    # Parse days of week
                    days_raw = row.get("Days of the Week", "").strip()
                    days = [d.strip() for d in days_raw.replace("+", ",").split(",") if d.strip()]

                    # Parse money values
                    target_raw = row.get("$ Target per week", "0")
                    target = Decimal(target_raw.replace("$", "").replace(",", "").strip() or "0")

                    is_csa = row.get("is_csa", "false").strip().lower() == "true"

                    data = {
                        "days_of_week": days,
                        "start_week": self._int(row.get("Start Week Num", 1)),
                        "end_week": self._int(row.get("End Week Num", 52)),
                        "weekly_target": target,
                        "is_csa": is_csa,
                        "allocation_priority": self._int(row.get("Priority", count + 1), count + 1),
                    }

                    if not dry_run:
                        SalesChannel.objects.update_or_create(name=name, defaults=data)

                    count += 1
                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"  Error on channel row {i}: {e}\n")
                    errors += 1

        self.stdout.write(f"  Channels: {count} imported, {errors} errors\n")

    # Helper methods for parsing messy spreadsheet data

    def _int(self, value, default=0):
        if not value:
            return default
        try:
            return int(float(str(value).strip()))
        except (ValueError, TypeError):
            return default

    def _int_or_none(self, value):
        if not value or str(value).strip() in ("", "0", "na", "NA"):
            return None
        try:
            result = int(float(str(value).strip()))
            return result if result > 0 else None
        except (ValueError, TypeError):
            return None

    def _dec(self, value, default="0"):
        if not value:
            return Decimal(default)
        try:
            cleaned = str(value).strip().replace("$", "").replace(",", "")
            return Decimal(cleaned) if cleaned else Decimal(default)
        except (InvalidOperation, TypeError):
            return Decimal(default)

    def _dec_or_none(self, value):
        if not value or str(value).strip() in ("", "0", "na", "NA"):
            return None
        try:
            cleaned = str(value).strip().replace("$", "").replace(",", "")
            result = Decimal(cleaned)
            return result if result > 0 else None
        except (InvalidOperation, TypeError):
            return None
