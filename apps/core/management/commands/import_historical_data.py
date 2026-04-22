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
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from datetime import datetime
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction, DatabaseError, IntegrityError

from reference.models import (
    CropInfo,
    Block,
    CropBySeason,
    CropSalesFormat,
    ProductRecipe,
    ProductRecipeComponent,
    SalesCategory,
    SalesChannel,
    SalesPlanBucket,
)
from reference.sales_rollups import ANNUAL_PLAN_SALES_CHANNELS
from planning.models import PlanningYear, Planting, NurseryEvent, HarvestEvent, PlantingStatus
from operations.models import (
    FieldWalkNote,
    InventoryLedger,
    PackAllocation,
    PackBatch,
    PackBatchComponent,
)
from sales.models import SalesEvent, QuickSalesEntry
from core.models import RotationHistory


class Command(BaseCommand):
    help = """Import 5 years of historical farm data from CSV files.
    
    Imports reference data, planning years, plantings, operations, and sales in
    dependency-aware order. Handles FK resolution by name, calculated fields,
    choice field mapping, and running balance sequencing."""
    FAILURE_SIGNATURE_OWNERSHIP = {
        "missing_required": {
            "owner_area": "data-contracts",
            "owner_team": "import-pipeline",
            "severity": "medium",
            "escalation_path": "ops-oncall -> data-contracts",
            "recovery": "populate required source fields and rerun --validate-only",
        },
        "namespace_mismatch": {
            "owner_area": "data-contracts",
            "owner_team": "import-pipeline",
            "severity": "medium",
            "escalation_path": "ops-oncall -> data-contracts",
            "recovery": "correct source value namespaces and rerun --validate-only",
        },
        "stale_fk": {
            "owner_area": "reference-data",
            "owner_team": "import-pipeline",
            "severity": "high",
            "escalation_path": "ops-oncall -> reference-data",
            "recovery": "seed missing reference rows and rerun --validate-only",
        },
        "fatal_import_exception": {
            "owner_area": "import-runtime",
            "owner_team": "platform",
            "severity": "high",
            "escalation_path": "ops-oncall -> platform",
            "recovery": "review fatal_error and importer logs before retry",
        },
        "unknown": {
            "owner_area": "triage",
            "owner_team": "platform",
            "severity": "high",
            "escalation_path": "ops-oncall -> platform",
            "recovery": "classify signature and add ownership mapping",
        },
    }
    # Stage A2 scaffold: declarative live-source normalizer contract.
    # Not wired to provider APIs yet; kept here to lock contract shape
    # without changing fixture-based offline workflow.
    LIVE_SOURCE_NORMALIZER_CONTRACT = {
        "schema_version": "a2-draft-1",
        "header_detection": {
            "strategy": "required_header_set_scan",
            "max_scan_rows": 200,
            "normalization": ["trim", "collapse_spaces", "casefold", "alias_lookup"],
            "fallbacks": ["anchor_token", "header_row_index"],
        },
        "output_layout": {
            "reference": "reference/*.csv",
            "yearly": "year_YYYY/*.csv",
            "manifest": "manifest.json",
        },
    }
    CHANNEL_ROLLUP_ALLOWED_VALUES = {"markets", "orders", "wholesale", "csa"}
    SALES_CATEGORY_PRIORITY = {
        SalesCategory.CategoryName.MARKETS: 10,
        SalesCategory.CategoryName.ORDERS: 20,
        SalesCategory.CategoryName.CSA: 30,
    }
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
            "--non-atomic-apply",
            action="store_true",
            help="Disable atomic transaction wrapping for apply mode (rollback safety off)",
        )
        parser.add_argument(
            "--validate-only",
            action="store_true",
            help="Run full validation preflight without writing data",
        )
        parser.add_argument(
            "--preflight",
            action="store_true",
            help="Alias for --validate-only",
        )
        parser.add_argument(
            "--summary-json",
            type=str,
            help="Write structured import summary artifact to this path",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Detailed per-row output",
        )

    def handle(self, *args, **options):
        self.data_dir = options["data_dir"]
        self.validate_only = bool(options["validate_only"] or options["preflight"])
        self.dry_run = bool(options["dry_run"])
        requested_non_atomic_apply = bool(options.get("non_atomic_apply"))
        if self.validate_only and self.dry_run:
            # Preflight mode takes precedence when both flags are provided.
            self.dry_run = False
        if self.validate_only:
            # Preflight always executes in a rollback transaction.
            self.atomic_apply = True
        elif self.dry_run:
            # Dry-run performs parse-only checks and never writes.
            self.atomic_apply = False
        else:
            self.atomic_apply = not requested_non_atomic_apply
        # Dry-run keeps legacy parse-only behavior; validate-only executes full flow in a rollback txn.
        self.write_disabled = self.dry_run
        self.verbose = options["verbose"]
        self.start_year = options["start_year"]
        self.end_year = options["end_year"]
        requested_summary_path = options.get("summary_json")
        self.run_started_at = datetime.utcnow()
        # Use microsecond precision to avoid artifact path collisions on rapid retries.
        self.run_id = self.run_started_at.strftime("%Y%m%dT%H%M%S%f")
        self.summary_json_path = self._resolve_summary_json_path(requested_summary_path)
        self.row_errors = []

        # Track statistics (legacy keys may still be populated in row paths).
        self.stats = defaultdict(
            lambda: {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "error": 0,
                "processed": 0,
                "errors": 0,
            }
        )

        # Cache for FK lookups
        self.crop_cache = {}
        self.block_cache = {}
        self.channel_cache = {}
        self.channel_name_aliases = {}
        self.channel_rollup_map = {}
        self.channel_rollup_required = False
        self.product_cache = {}
        self.recipe_cache = {}
        self.planning_year_cache = {}
        self.planting_cache = {}
        self.harvest_event_cache = {}
        self.pack_batch_cache = {}
        self.normalized_lookup_indexes = {}

        if not os.path.isdir(self.data_dir):
            raise CommandError(f"Data directory not found: {self.data_dir}")

        if self.validate_only:
            self.stdout.write(self.style.WARNING("\n⚠️  VALIDATE-ONLY/PREFLIGHT — no data will be saved\n"))
        elif self.dry_run:
            self.stdout.write(self.style.WARNING("\n⚠️  DRY-RUN — parse/shape checks only, no data will be saved\n"))

        try:
            if self.validate_only:
                # Preflight mode executes full mapping/validation path and then rolls back.
                with transaction.atomic():
                    self._run_import_pipeline()
                    transaction.set_rollback(True)
            elif self.atomic_apply and not self.dry_run:
                with transaction.atomic():
                    self._run_import_pipeline()
            else:
                self._run_import_pipeline()

            self.stdout.write(self.style.SUCCESS("\n" + "=" * 70))
            self._print_summary()
            self._write_summary_json(status="ok")
            self.stdout.write("=" * 70 + "\n")

        except Exception as e:
            fatal_error = self._format_fatal_error(e)
            self.stderr.write(self.style.ERROR(f"\n❌ FATAL ERROR: {fatal_error}"))
            self._write_summary_json(status="failed", fatal_error=fatal_error)
            self.stderr.write(
                self.style.WARNING(
                    f"   ↳ recovery: inspect '{self.summary_json_path}' for row/failure signatures before retry"
                )
            )
            if self.atomic_apply and not self.validate_only:
                self.stderr.write(
                    self.style.WARNING(
                        "   ↳ recovery: rerun with --non-atomic-apply to preserve partial writes for diagnostics"
                    )
                )
            if self.verbose:
                import traceback
                traceback.print_exc()
            sys.exit(1)

    def _run_import_pipeline(self):
        self._load_channel_name_aliases()
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

    def _resolve_summary_json_path(self, requested_path):
        if requested_path:
            return requested_path
        artifact_dir = Path(self.data_dir) / "_import_artifacts"
        return str(artifact_dir / f"historical-import-summary-{self.run_id}.json")

    def _record_row_error(self, model_name, row_number, code, field_path, message):
        """Capture structured row-level error details for summary artifacts."""
        self.row_errors.append(
            {
                "model": model_name,
                "row": row_number,
                "code": code,
                "field_path": field_path,
                "message": str(message),
            }
        )

    def _record_stale_fk(self, model_name, row_number, field_path, missing_label, raw_value):
        """Record a stale FK row error with a consistent message shape."""
        message = f"{missing_label} not found '{raw_value}'"
        self.stderr.write(f"    ERROR row {row_number}: {message}")
        self._record_row_error(
            model_name,
            row_number,
            code="stale_fk",
            field_path=field_path,
            message=message,
        )

    def _record_missing_required(self, model_name, row_number, field_path, field_label):
        """Record deterministic required-field gaps as structured row errors."""
        message = f"missing required value for '{field_label}'"
        self.stderr.write(f"    ERROR row {row_number}: {message}")
        self._record_row_error(
            model_name,
            row_number,
            code="missing_required",
            field_path=field_path,
            message=message,
        )

    def _normalize_rollup_group_to_category(self, group_name):
        normalized = str(group_name or "").strip().casefold()
        if normalized in {"markets", "market"}:
            return SalesCategory.CategoryName.MARKETS
        if normalized in {"orders", "order", "wholesale"}:
            return SalesCategory.CategoryName.ORDERS
        if normalized == "csa":
            return SalesCategory.CategoryName.CSA
        return None

    def _category_for_sales_channel(self, channel_name, is_csa):
        if is_csa:
            return SalesCategory.CategoryName.CSA
        exact = str(channel_name or "").strip()
        for plan_name, cat in ANNUAL_PLAN_SALES_CHANNELS:
            if exact == plan_name:
                return cat
        if exact.casefold() in {"wholesale", "orders"}:
            return SalesCategory.CategoryName.ORDERS
        return SalesCategory.CategoryName.MARKETS

    def _ensure_sales_category(self, category_name):
        return SalesCategory.objects.update_or_create(
            name=category_name,
            defaults={"allocation_priority": self.SALES_CATEGORY_PRIORITY[category_name]},
        )[0]

    def get_live_source_normalizer_contract(self):
        """Expose Stage A2 normalizer contract scaffold for future connector work."""
        return self.LIVE_SOURCE_NORMALIZER_CONTRACT

    # ============================================================================
    # TIER 1: Reference Data (Independent)
    # ============================================================================

    def _import_reference_tier(self):
        """Import reference data: blocks, crops, crop_by_season, channels, products."""
        self._import_blocks()
        self._import_crops()
        self._ensure_placeholder_crops_for_sales_format_catalog()
        self._import_crop_by_season()
        self._import_sales_channels()
        self._load_channel_rollup_contract()
        self._ensure_annual_plan_sales_channels_if_needed()
        self._import_crop_sales_formats()
        self._import_product_recipe_components()
        self._warm_recipe_cache()

    def _load_channel_name_aliases(self):
        """Optional reference/channel_name_aliases.csv: map planner/sheet labels to SalesChannel.name."""
        path = self._resolve_reference_path("channel_name_aliases.csv")
        if not os.path.exists(path):
            return
        self.stdout.write("Loading channel name aliases...")
        loaded = 0
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                alias = (row.get("Alias") or row.get("alias") or "").strip()
                canonical = (
                    (row.get("Channel Name") or row.get("channel_name") or row.get("Canonical") or "")
                    .strip()
                )
                if not alias or not canonical:
                    continue
                key = self._normalize_lookup_value(alias)
                if key in self.channel_name_aliases and self.channel_name_aliases[key] != canonical:
                    raise CommandError(
                        f"channel_name_aliases.csv row {i}: conflicting canonical channel for alias '{alias}'"
                    )
                self.channel_name_aliases[key] = canonical
                loaded += 1
        self.stdout.write(f"  loaded {loaded} channel name aliases\n")

    def _load_channel_rollup_contract(self):
        """Load optional specific-channel -> rollup-group mapping contract."""
        path = self._resolve_reference_path("channel_rollups.csv")
        if not os.path.exists(path):
            self.channel_rollup_required = False
            return

        self.stdout.write("Loading channel rollup contract...")
        loaded = {}
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                channel_name = (row.get("Channel Name") or "").strip()
                raw_group = (row.get("Rollup Group") or "").strip()
                if not channel_name:
                    continue
                if not raw_group:
                    raise CommandError(
                        f"channel_rollups.csv row {i}: missing Rollup Group for '{channel_name}'"
                    )
                category_name = self._normalize_rollup_group_to_category(raw_group)
                if category_name is None:
                    allowed = ", ".join(sorted(v.title() for v in self.CHANNEL_ROLLUP_ALLOWED_VALUES))
                    raise CommandError(
                        f"channel_rollups.csv row {i}: invalid Rollup Group '{raw_group}' "
                        f"for '{channel_name}' (allowed: {allowed})"
                    )
                canonical_group = category_name
                if channel_name in loaded and loaded[channel_name] != canonical_group:
                    raise CommandError(
                        f"channel_rollups.csv row {i}: conflicting Rollup Group for '{channel_name}'"
                    )
                loaded[channel_name] = canonical_group

        self.channel_rollup_map = loaded
        self.channel_rollup_required = True
        if not self.write_disabled:
            default_bucket_by_category = {
                SalesCategory.CategoryName.ORDERS: "Wholesale",
                SalesCategory.CategoryName.MARKETS: "Markets",
                SalesCategory.CategoryName.CSA: "CSA",
            }
            for channel_name, category_name in self.channel_rollup_map.items():
                category = self._ensure_sales_category(category_name)
                plan_bucket = SalesPlanBucket.objects.filter(
                    category=category,
                    name=channel_name,
                ).first()
                if plan_bucket is None:
                    default_bucket_name = default_bucket_by_category.get(category_name)
                    if default_bucket_name:
                        plan_bucket = SalesPlanBucket.objects.filter(
                            category=category,
                            name=default_bucket_name,
                        ).first()
                channel_defaults = {
                    "category": category,
                    "plan_bucket": plan_bucket,
                    "days_of_week": [],
                    "start_week": 1,
                    "end_week": 52,
                    "weekly_target": Decimal("0"),
                    "is_csa": category_name == SalesCategory.CategoryName.CSA,
                    "allocation_priority": self.SALES_CATEGORY_PRIORITY[category_name],
                }
                channel_obj, _ = SalesChannel.objects.update_or_create(
                    name=channel_name,
                    defaults=channel_defaults,
                )
                self.channel_cache[channel_name] = channel_obj
        self.stdout.write(f"  loaded {len(self.channel_rollup_map)} channel rollup assignments\n")

    def _bundle_has_product_week_plan_csv(self):
        """True when this bundle carries annual product/week demand rows (301-style grids)."""
        data_path = Path(self.data_dir)
        if not data_path.is_dir():
            return False
        return any(data_path.glob("year_*/product_week_plan.csv"))

    def _ensure_annual_plan_sales_channels_if_needed(self):
        """Only seed rollup-level planning channels when product_week_plan data is present."""
        if not self._bundle_has_product_week_plan_csv():
            return
        self._ensure_annual_plan_sales_channels()

    def _ensure_annual_plan_sales_channels(self):
        """Create rollup-level SalesChannel rows for annual plan grids (301 Markets/Orders tabs).

        Operational outlets (BFM, KFM, Wholesale, …) roll up into Markets vs Orders categories;
        the wide planning worksheets are category targets, not a single outlet.
        """
        if self.write_disabled:
            return
        self.stdout.write("Ensuring annual-plan sales channels (rollup-level planning)...")
        default_bucket_by_category = {
            SalesCategory.CategoryName.ORDERS: "Wholesale",
            SalesCategory.CategoryName.MARKETS: "Markets",
            SalesCategory.CategoryName.CSA: "CSA",
        }
        for channel_name, category_name in ANNUAL_PLAN_SALES_CHANNELS:
            category = self._ensure_sales_category(category_name)
            default_bucket_name = default_bucket_by_category.get(category_name)
            plan_bucket = SalesPlanBucket.objects.filter(category=category, name=channel_name).first()
            if plan_bucket is None and default_bucket_name:
                plan_bucket = SalesPlanBucket.objects.filter(
                    category=category,
                    name=default_bucket_name,
                ).first()
            channel_defaults = {
                "category": category,
                "plan_bucket": plan_bucket,
                "days_of_week": [],
                "start_week": 1,
                "end_week": 52,
                "weekly_target": Decimal("0"),
                "is_csa": category_name == SalesCategory.CategoryName.CSA,
                "allocation_priority": self.SALES_CATEGORY_PRIORITY[category_name],
            }
            channel_obj, created_flag = SalesChannel.objects.update_or_create(
                name=channel_name,
                defaults=channel_defaults,
            )
            self.channel_cache[channel_name] = channel_obj
            self.channel_rollup_map[channel_name] = category_name
            if not self.write_disabled:
                key = "created" if created_flag else "processed"
                self.stats["SalesChannel"][key] += 1
        self.stdout.write(f"  ensured {len(ANNUAL_PLAN_SALES_CHANNELS)} annual-plan channels\n")

    def _has_channel_rollup_assignment(self, model_name, row_number, field_path, channel_name):
        if not self.channel_rollup_required:
            return True
        if channel_name in self.channel_rollup_map:
            return True
        message = f"missing rollup assignment for sales channel '{channel_name}' in channel_rollups.csv"
        self.stderr.write(f"    ERROR row {row_number}: {message}")
        self._record_row_error(
            model_name,
            row_number,
            code="missing_required",
            field_path=field_path,
            message=message,
        )
        return False

    def _resolve_reference_path(self, filename):
        """Support both legacy root-level fixtures and Stage A2 `reference/` bundles."""
        root_path = os.path.join(self.data_dir, filename)
        if os.path.exists(root_path):
            return root_path
        return os.path.join(self.data_dir, "reference", filename)

    def _import_blocks(self):
        """Import block definitions."""
        path = self._resolve_reference_path("blocks.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ blocks.csv not found\n")
            return

        self.stdout.write("Importing blocks...")

        type_map = {
            "field": "field",
            "high tunnel": "high_tunnel",
            "greenhouse": "greenhouse",
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

                    if not self.write_disabled:
                        obj, created = Block.objects.update_or_create(name=name, defaults=data)
                        self.stats["Block"]["created" if created else "processed"] += 1
                        self.block_cache[name] = obj
                    else:
                        self.block_cache[name] = name
                        self.stats["Block"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["Block"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['Block']['processed']} processed, "
            f"{self.stats['Block']['errors']} errors\n"
        )

    def _import_crops(self):
        """Import crop info."""
        path = self._resolve_reference_path("crop_info.csv")
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

                    if not self.write_disabled:
                        obj, created = CropInfo.objects.update_or_create(name=name, defaults=data)
                        self.stats["CropInfo"]["created" if created else "processed"] += 1
                        self.crop_cache[name] = obj
                    else:
                        self.crop_cache[name] = name
                        self.stats["CropInfo"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["CropInfo"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['CropInfo']['processed']} processed, "
            f"{self.stats['CropInfo']['skipped']} skipped, "
            f"{self.stats['CropInfo']['errors']} errors\n"
        )

    def _ensure_placeholder_crops_for_sales_format_catalog(self):
        """
        Workbook 201 ``Crop Info`` may omit sellable-only crops that still appear as ``Crop Name``
        on ``Farm Crop Formats`` / ``Design Crop Mixes`` (e.g. ``Braising Mix``). Create minimal
        ``CropInfo`` rows so ``crop_sales_formats`` and mix recipes can resolve FKs.

        Guard: only when ``crop_info.csv`` already looks like a full farm catalog (enough rows).
        Small contract fixtures intentionally omit crops to exercise ``stale_fk`` paths; they must
        not pick up automatic placeholders from ``crop_sales_formats.csv`` alone.
        """
        path = self._resolve_reference_path("crop_sales_formats.csv")
        if not os.path.exists(path):
            return
        if self.write_disabled:
            return

        catalog_path = self._resolve_reference_path("crop_info.csv")
        catalog_crop_names = set()
        if os.path.exists(catalog_path):
            with open(catalog_path, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    n = (row.get("Crop") or "").strip()
                    if n:
                        catalog_crop_names.add(n)
        if len(catalog_crop_names) < 20:
            return

        names = set()
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cn = (row.get("Crop Name") or row.get("Crop") or "").strip()
                if cn:
                    names.add(cn)
        if not names:
            return

        defaults = {
            "crop_type": "Vegetables",
            "botanical_family": "",
            "propagation_type": "seed",
            "is_perennial": False,
            "fresh_or_storage": "fresh",
            "storage_weeks": 0,
            "can_hold_in_field": False,
            "harvest_unit": "pounds",
            "avg_unit_weight": Decimal("1"),
            "units_per_bin": None,
            "harvest_bin": "",
            "harvest_tools": "",
            "harvest_rate_per_hour": None,
            "nursery_weeks": 0,
            "weeks_until_pot_up": 0,
            "pot_up_tray_size": None,
            "seeded_tray_size": None,
            "seeds_per_cell": 1,
            "thinned_plants": 0,
            "seeds_per_ounce": None,
        }

        created = 0
        for name in sorted(names):
            if name in catalog_crop_names:
                continue
            if CropInfo.objects.filter(name=name).exists():
                continue
            obj = CropInfo.objects.create(name=name, **defaults)
            created += 1
            self.crop_cache[name] = obj

        if created:
            self.normalized_lookup_indexes.clear()
            self.stdout.write(
                f"  placeholder CropInfo rows for catalog holes: {created} created "
                f"(from crop_sales_formats.csv crop names)\n"
            )

    def _import_crop_by_season(self):
        """Import crop-by-season profiles."""
        path = self._resolve_reference_path("crop_by_season.csv")
        if not os.path.exists(path):
            self.stdout.write(f"  ⊘ crop_by_season.csv not found\n")
            return

        self.stdout.write("Importing crop by season...")

        # Keys stay normalized because incoming values are casefolded/collapsed.
        type_map = {
            "field": "field",
            "high tunnel": "high_tunnel",
            "greenhouse": "greenhouse",
        }

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    crop_name = row["Crop"].strip()
                    block_type_raw = row["Block Type"].strip()

                    if not crop_name:
                        continue

                    normalized_block_type = " ".join(block_type_raw.split()).casefold()
                    block_type = type_map.get(normalized_block_type)
                    if not block_type:
                        message = f"unsupported block type '{block_type_raw}'"
                        self.stderr.write(f"    ERROR row {i}: {message}")
                        self._record_row_error(
                            "CropBySeason",
                            i,
                            code="namespace_mismatch",
                            field_path="crop_by_season.block_type",
                            message=message,
                        )
                        self.stats["CropBySeason"]["errors"] += 1
                        continue

                    crop = self._get_crop(crop_name)
                    if not crop:
                        message = f"crop not found '{crop_name}'"
                        self.stderr.write(f"    ERROR row {i}: {message}")
                        self._record_row_error(
                            "CropBySeason",
                            i,
                            code="stale_fk",
                            field_path="crop_by_season.crop",
                            message=message,
                        )
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

                    if not self.write_disabled:
                        obj, created = CropBySeason.objects.update_or_create(
                            crop=crop, block_type=block_type, defaults=data
                        )
                        self.stats["CropBySeason"]["created" if created else "processed"] += 1
                    else:
                        self.stats["CropBySeason"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["CropBySeason"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['CropBySeason']['processed']} processed, "
            f"{self.stats['CropBySeason']['skipped']} skipped, "
            f"{self.stats['CropBySeason']['errors']} errors\n"
        )

    def _import_sales_channels(self):
        """Import sales channels."""
        path = self._resolve_reference_path("sales_channels.csv")
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
                    category_name = self._category_for_sales_channel(name, is_csa)

                    data = {
                        "days_of_week": days,
                        "start_week": self._int(row.get("Start Week Num", 1)),
                        "end_week": self._int(row.get("End Week Num", 52)),
                        "weekly_target": target,
                        "is_csa": is_csa,
                        "allocation_priority": self._int(row.get("Priority", i), i),
                    }

                    if not self.write_disabled:
                        category = self._ensure_sales_category(category_name)
                        plan_bucket, _ = SalesPlanBucket.objects.update_or_create(
                            name=name,
                            defaults={
                                "category": category,
                                "start_week": data["start_week"],
                                "end_week": data["end_week"],
                                "weekly_target": data["weekly_target"],
                                "allocation_priority": data["allocation_priority"],
                                "is_active": True,
                            },
                        )
                        data["category"] = category
                        data["plan_bucket"] = plan_bucket
                        obj, created = SalesChannel.objects.update_or_create(
                            name=name, defaults=data
                        )
                        self.stats["SalesChannel"]["created" if created else "processed"] += 1
                        self.channel_cache[name] = obj
                    else:
                        self.channel_cache[name] = name
                        self.stats["SalesChannel"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["SalesChannel"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['SalesChannel']['processed']} processed, "
            f"{self.stats['SalesChannel']['errors']} errors\n"
        )

    def _import_crop_sales_formats(self):
        """Import crop sales formats (products)."""
        path = self._resolve_reference_path("crop_sales_formats.csv")
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
                        message = f"crop not found '{crop_name}'"
                        self.stderr.write(f"    ERROR row {i}: {message}")
                        self._record_row_error(
                            "CropSalesFormat",
                            i,
                            code="stale_fk",
                            field_path="crop_sales_formats.crop",
                            message=message,
                        )
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

                    if not self.write_disabled:
                        obj, created = CropSalesFormat.objects.update_or_create(
                            crop=crop, product_name=product_name, defaults=data
                        )
                        self.stats["CropSalesFormat"]["created" if created else "processed"] += 1
                        self.product_cache[(crop_name, product_name)] = obj
                    else:
                        self.product_cache[(crop_name, product_name)] = (crop_name, product_name)
                        self.stats["CropSalesFormat"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["CropSalesFormat"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['CropSalesFormat']['processed']} processed, "
            f"{self.stats['CropSalesFormat']['errors']} errors\n"
        )

    def _prc_field(self, row, *candidates):
        for name in candidates:
            if not name or name not in row:
                continue
            val = row.get(name)
            if val is None:
                continue
            s = str(val).strip()
            if s:
                return s
        return ""

    def _csf_matches_for_mix_label(self, crop, mix_product):
        """
        Map workbook short mix label (``Choose Mix``) to ``CropSalesFormat`` rows.

        Farm formats typically use ``{label} - {pack size}`` while recipe rows use ``label`` only.
        """
        qs = CropSalesFormat.objects.all()
        if crop is not None:
            qs = qs.filter(crop=crop)
        exact = list(qs.filter(product_name=mix_product).order_by("id"))
        if exact:
            return exact
        prefix = f"{mix_product} -"
        return list(qs.filter(product_name__startswith=prefix).order_by("id"))

    def _pick_unique_mix_csf(self, matches, model_name, i, mix_label_for_error):
        if len(matches) == 1:
            return matches[0], False
        if not matches:
            self._record_stale_fk(
                model_name,
                i,
                "product_recipe_components.mix_product",
                "product",
                mix_label_for_error,
            )
            return None, True
        label = str(mix_label_for_error).split("/", 1)[-1].strip()
        same_crop_as_label = [m for m in matches if m.crop.name == label]
        if len(same_crop_as_label) == 1:
            return same_crop_as_label[0], False
        crops_seen = {m.crop_id for m in matches}
        if len(crops_seen) == 1:
            return matches[0], False
        self._record_row_error(
            model_name,
            i,
            code="stale_fk",
            field_path="product_recipe_components.mix_product",
            message=(
                f"multiple products match label '{mix_label_for_error}' with different crops; "
                "set Mix Crop Name to disambiguate"
            ),
        )
        return None, True

    def _resolve_csf_for_mix_row(self, row, i, model_name="ProductRecipeComponent"):
        """
        Resolve CropSalesFormat for a product_recipe_components row (read-only lookups).
        Returns (csf|None, error_recorded: bool).
        """
        mix_product = self._prc_field(
            row, "Mix Product Name", "Mix Product", "Choose Mix"
        )
        if not mix_product:
            return None, False
        mix_crop = self._prc_field(row, "Mix Crop Name", "Mix Crop")
        if mix_crop:
            crop = self._get_crop(mix_crop)
            if not crop:
                self._record_stale_fk(
                    model_name,
                    i,
                    "product_recipe_components.mix_crop",
                    "crop",
                    mix_crop,
                )
                return None, True
            matches = self._csf_matches_for_mix_label(crop, mix_product)
            return self._pick_unique_mix_csf(
                matches, model_name, i, f"{mix_crop} / {mix_product}"
            )
        matches = self._csf_matches_for_mix_label(None, mix_product)
        return self._pick_unique_mix_csf(matches, model_name, i, mix_product)

    def _import_product_recipe_components(self):
        """Import mix recipe lines from reference/product_recipe_components.csv."""
        path = self._resolve_reference_path("product_recipe_components.csv")
        if not os.path.exists(path):
            self.stdout.write("  ⊘ product_recipe_components.csv not found\n")
            return

        self.stdout.write("Importing product recipe components...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            rows_list = list(enumerate(reader, 1))

        buckets = defaultdict(list)
        csf_by_id = {}
        for i, row in rows_list:
            csf, err = self._resolve_csf_for_mix_row(row, i)
            if err:
                self.stats["ProductRecipeComponent"]["errors"] += 1
                continue
            if not self._prc_field(row, "Mix Product Name", "Mix Product", "Choose Mix"):
                self.stats["ProductRecipeComponent"]["skipped"] += 1
                continue
            if not csf:
                self.stats["ProductRecipeComponent"]["skipped"] += 1
                continue
            buckets[csf.id].append((i, row))
            csf_by_id[csf.id] = csf

        for pk, rows_group in buckets.items():
            csf = csf_by_id[pk]
            recipe_names = set()
            for _i, r in rows_group:
                rn = self._prc_field(r, "Recipe Name")
                if rn:
                    recipe_names.add(rn)
            if len(recipe_names) > 1:
                for row_num, row in rows_group:
                    self._record_row_error(
                        "ProductRecipe",
                        row_num,
                        code="namespace_mismatch",
                        field_path="product_recipe_components.recipe_name",
                        message="conflicting Recipe Name values for the same mix product",
                    )
                    self.stats["ProductRecipe"]["errors"] += 1
                continue
            recipe_final = recipe_names.pop() if recipe_names else "Default"
            ordered = sorted(
                rows_group, key=lambda t: self._prc_sort_key(t[1], t[0])
            )
            self._prc_process_recipe_group(ordered, csf, recipe_final)

        rp = self.stats["ProductRecipe"]
        rc = self.stats["ProductRecipeComponent"]
        self.stdout.write(
            f" {rp.get('processed', 0) + rp.get('created', 0)} recipes, "
            f"{rc.get('processed', 0) + rc.get('created', 0)} components, "
            f"{rp.get('errors', 0)} recipe errors, "
            f"{rc.get('errors', 0)} component errors\n"
        )

    def _prc_sort_key(self, row, row_num):
        lo = self._prc_field(row, "Line Order")
        if lo:
            try:
                return (0, int(lo))
            except ValueError:
                pass
        return (1, row_num)

    def _prc_process_recipe_group(self, ordered_pairs, csf, recipe_final):
        """Create or replace one ProductRecipe and its ProductRecipeComponents."""
        model_rc = "ProductRecipeComponent"
        if self.write_disabled:
            self.stats["ProductRecipe"]["processed"] += 1
            self.stats["ProductRecipeComponent"]["processed"] += len(ordered_pairs)
            return
        recipe_final = recipe_final or "Default"
        ProductRecipe.objects.filter(product=csf).update(is_active=False)
        recipe, created = ProductRecipe.objects.update_or_create(
            product=csf,
            name=recipe_final,
            defaults={
                "is_active": True,
                "output_unit": "",
                "notes": "Imported from product_recipe_components.csv",
            },
        )
        self.stats["ProductRecipe"]["created" if created else "processed"] += 1

        recipe.components.all().delete()

        for idx, (row_num, row) in enumerate(ordered_pairs, start=1):
            source_type_raw = self._prc_field(
                row, "Component Source Type", "Source Type"
            )
            if not source_type_raw:
                self._record_missing_required(
                    model_rc,
                    row_num,
                    "product_recipe_components.component_source_type",
                    "Component Source Type",
                )
                self.stats["ProductRecipeComponent"]["errors"] += 1
                continue
            st = source_type_raw.strip().lower()
            if st in ("c", "crop", "vegetable"):
                source_kind = "crop"
            elif st in ("p", "product"):
                source_kind = "product"
            else:
                self._record_row_error(
                    model_rc,
                    row_num,
                    code="namespace_mismatch",
                    field_path="product_recipe_components.component_source_type",
                    message=f"expected crop or product, got '{source_type_raw}'",
                )
                self.stats["ProductRecipeComponent"]["errors"] += 1
                continue

            pct_raw = self._prc_field(row, "Component Percent", "Percent")
            qty_raw = self._prc_field(
                row, "Component Quantity", "Quantity", "Qty"
            )
            unit_raw = self._prc_field(row, "Component Unit", "Unit")

            pct = None
            if pct_raw:
                try:
                    pct = self._dec(pct_raw)
                    if pct <= 0 or pct > Decimal("100"):
                        raise ValueError("percent range")
                except (InvalidOperation, ValueError):
                    self._record_row_error(
                        model_rc,
                        row_num,
                        code="namespace_mismatch",
                        field_path="product_recipe_components.component_percent",
                        message=f"invalid percent '{pct_raw}'",
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue

            qty = None
            if qty_raw:
                try:
                    qty = self._dec(qty_raw)
                    if qty <= 0:
                        self.stats["ProductRecipeComponent"]["skipped"] += 1
                        continue
                    qty = qty.quantize(Decimal("0.01"))
                except (InvalidOperation, ValueError):
                    self._record_row_error(
                        model_rc,
                        row_num,
                        code="namespace_mismatch",
                        field_path="product_recipe_components.component_quantity",
                        message=f"invalid quantity '{qty_raw}'",
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue

            if pct is None and qty is None:
                # Blank template / spacer lines on workbook 202 ``Design Crop Mixes`` tab.
                self.stats["ProductRecipeComponent"]["skipped"] += 1
                continue

            if qty is not None and not unit_raw:
                self._record_missing_required(
                    model_rc,
                    row_num,
                    "product_recipe_components.component_unit",
                    "Component Unit",
                )
                self.stats["ProductRecipeComponent"]["errors"] += 1
                continue

            if pct is not None and qty is None:
                qty = Decimal("1")
                unit_raw = unit_raw or csf.sale_unit

            if qty is not None and pct is None and not unit_raw:
                unit_raw = csf.sale_unit

            comp_crop = self._prc_field(
                row, "Component Crop Name", "Component Crop", "Choose Ingredients"
            )
            comp_product = self._prc_field(
                row, "Component Product Name", "Component Product"
            )

            src_crop = None
            src_product = None
            if source_kind == "crop":
                if comp_product:
                    self._record_row_error(
                        model_rc,
                        row_num,
                        code="namespace_mismatch",
                        field_path="product_recipe_components.component_product",
                        message="Component Product Name must be empty when source type is crop",
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue
                if not comp_crop:
                    self._record_missing_required(
                        model_rc,
                        row_num,
                        "product_recipe_components.component_crop",
                        "Component Crop Name",
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue
                crop_obj = self._get_crop(comp_crop)
                if not crop_obj:
                    self._record_stale_fk(
                        model_rc,
                        row_num,
                        "product_recipe_components.component_crop",
                        "crop",
                        comp_crop,
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue
                src_crop = crop_obj
            else:
                if not comp_crop or not comp_product:
                    self._record_missing_required(
                        model_rc,
                        row_num,
                        "product_recipe_components.component_product",
                        "Component Crop Name and Component Product Name",
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue
                c_crop = self._get_crop(comp_crop)
                if not c_crop:
                    self._record_stale_fk(
                        model_rc,
                        row_num,
                        "product_recipe_components.component_crop",
                        "crop",
                        comp_crop,
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue
                csf_comp = CropSalesFormat.objects.filter(
                    crop=c_crop, product_name=comp_product
                ).first()
                if not csf_comp:
                    self._record_stale_fk(
                        model_rc,
                        row_num,
                        "product_recipe_components.component_product",
                        "product",
                        f"{comp_crop} / {comp_product}",
                    )
                    self.stats["ProductRecipeComponent"]["errors"] += 1
                    continue
                src_product = csf_comp

            comp_obj = ProductRecipeComponent(
                recipe=recipe,
                source_crop=src_crop,
                source_product=src_product,
                component_quantity=qty,
                component_unit=unit_raw,
                component_percent=pct,
                sort_order=idx,
                notes=self._prc_field(row, "Notes"),
            )
            try:
                comp_obj.full_clean()
                comp_obj.save()
                self.stats["ProductRecipeComponent"]["created"] += 1
            except (
                ValidationError,
                IntegrityError,
                DatabaseError,
            ) as e:
                self._record_row_error(
                    model_rc,
                    row_num,
                    code="namespace_mismatch",
                    field_path="product_recipe_components.row",
                    message=str(e),
                )
                self.stats["ProductRecipeComponent"]["errors"] += 1
                continue

        try:
            recipe.refresh_from_db()
            recipe.clean()
            recipe.validate_component_totals()
        except ValidationError as e:
            first_row = ordered_pairs[0][0]
            self._record_row_error(
                "ProductRecipe",
                first_row,
                code="namespace_mismatch",
                field_path="product_recipe_components.recipe_validation",
                message=str(e),
            )
            self.stats["ProductRecipe"]["errors"] += 1
            recipe.components.all().delete()
            recipe.delete()

    def _warm_recipe_cache(self):
        """Populate recipe lookup cache for tier 4/5 mix resolution."""
        if self.write_disabled:
            return
        for rec in ProductRecipe.objects.select_related("product").iterator():
            self.recipe_cache[(rec.product_id, rec.name)] = rec

    def _materialize_pack_batch_components(self, pack_batch, recipe, row_num):
        """
        Scale recipe lines to PackBatchComponent rows (per recipe output unit).
        Returns False if the batch could not be materialized (batch is removed).
        """
        if self.write_disabled:
            return True
        output_u = (
            recipe.output_unit or recipe.product.sale_unit or ""
        ).strip().casefold()
        pack_u = (pack_batch.packed_unit or "").strip().casefold()
        if output_u != pack_u:
            self._record_row_error(
                "PackAllocation",
                row_num,
                code="namespace_mismatch",
                field_path="pack_allocations.packed_unit",
                message=(
                    f"packed unit '{pack_batch.packed_unit}' does not match recipe output unit "
                    f"'{recipe.output_unit or recipe.product.sale_unit}'"
                ),
            )
            pack_batch.delete()
            return False

        pack_batch.components.all().delete()
        factor = pack_batch.packed_quantity
        for rc in recipe.components.select_related(
            "source_crop", "source_product"
        ).order_by("sort_order", "id"):
            consumed = rc.component_quantity * factor
            PackBatchComponent.objects.create(
                pack_batch=pack_batch,
                source_crop=rc.source_crop,
                source_product=rc.source_product,
                consumed_quantity=consumed,
                consumed_unit=rc.component_unit,
                component_percent=rc.component_percent,
            )
        return True

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
            self._import_product_week_plan(year, year_dir)
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

                    if not self.write_disabled:
                        obj, created = PlanningYear.objects.update_or_create(
                            year=py_year, defaults=data
                        )
                        self.stats["PlanningYear"]["created" if created else "processed"] += 1
                        self.planning_year_cache[py_year] = obj
                    else:
                        self.planning_year_cache[py_year] = py_year
                        self.stats["PlanningYear"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
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
                        if not planning_year:
                            self._record_stale_fk(
                                "Planting",
                                i,
                                "plantings.planning_year",
                                "planning year",
                                year,
                            )
                        if not crop:
                            self._record_stale_fk(
                                "Planting",
                                i,
                                "plantings.crop",
                                "crop",
                                crop_name,
                            )
                        if not block:
                            self._record_stale_fk(
                                "Planting",
                                i,
                                "plantings.block",
                                "block",
                                block_name,
                            )
                        self.stats["Planting"]["errors"] += 1
                        continue

                    # Crop season is required for Planting in all modes.
                    crop_season = self._get_crop_season(crop, block.block_type)
                    if not crop_season:
                        self.stdout.write(
                            self.style.WARNING(
                                f"   ⚠  row {i}: no crop_season for {crop_name}/{block.block_type} — skipping planting"
                            )
                        )
                        self.stats["Planting"]["skipped"] += 1
                        continue

                    # Parse dates
                    plant_date_str = row.get("Planned Plant Date", "").strip()
                    if not plant_date_str:
                        self.stats["Planting"]["skipped"] += 1
                        continue

                    plant_date = self._parse_date(plant_date_str)

                    data = {
                        "crop_season": crop_season,
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

                    if not self.write_disabled:
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

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["Planting"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['Planting']['processed']} processed, "
            f"{self.stats['Planting']['skipped']} skipped, "
            f"{self.stats['Planting']['errors']} errors\n"
        )

    def _import_product_week_plan(self, year, year_dir):
        """Import product-week demand plan into SalesEvent plan rows."""
        path = os.path.join(year_dir, "product_week_plan.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing product-week sales plan {year}...")

        planning_year = self._get_planning_year(year)
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    channel_name = (row.get("Channel Name") or row.get("Channel") or "").strip()
                    product_name = (row.get("Product Name") or row.get("Product") or "").strip()
                    week_raw = (row.get("Week") or "").strip()
                    quantity_raw = (row.get("Planned Quantity") or "").strip()

                    if not channel_name:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "product_week_plan.channel",
                            "Channel Name",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not product_name:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "product_week_plan.product",
                            "Product Name",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not week_raw:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "product_week_plan.week",
                            "Week",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not quantity_raw:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "product_week_plan.planned_quantity",
                            "Planned Quantity",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    channel = self._get_channel(channel_name)
                    if not channel:
                        self._record_stale_fk(
                            "SalesEvent",
                            i,
                            "product_week_plan.channel",
                            "sales channel",
                            channel_name,
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not self._has_channel_rollup_assignment(
                        "SalesEvent",
                        i,
                        "product_week_plan.channel_rollup",
                        channel.name,
                    ):
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    product = self._get_product_by_name(product_name)
                    if not product:
                        self._record_stale_fk(
                            "SalesEvent",
                            i,
                            "product_week_plan.product",
                            "product",
                            product_name,
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    week = self._int(week_raw, 0)
                    if week < 1 or week > 53:
                        message = f"invalid week '{week_raw}'"
                        self.stderr.write(f"    ERROR row {i}: {message}")
                        self._record_row_error(
                            "SalesEvent",
                            i,
                            code="namespace_mismatch",
                            field_path="product_week_plan.week",
                            message=message,
                        )
                        self.stats["SalesEvent"]["errors"] += 1
                        continue

                    sale_date = datetime.fromisocalendar(year, week, 1).date()
                    planned_quantity = self._dec(quantity_raw)
                    planned_revenue_raw = (row.get("Planned Revenue") or "").strip()
                    if planned_revenue_raw:
                        planned_revenue = self._dec(planned_revenue_raw)
                    else:
                        planned_revenue = planned_quantity * product.sale_price

                    defaults = {
                        "planning_year": planning_year,
                        "entry_kind": SalesEvent.EntryKind.PLAN,
                        "planned_quantity": planned_quantity,
                        "planned_revenue": planned_revenue,
                        "notes": (row.get("Notes") or "").strip(),
                    }

                    if not self.write_disabled:
                        _, created = SalesEvent.objects.update_or_create(
                            entry_kind=SalesEvent.EntryKind.PLAN,
                            channel=channel,
                            sale_date=sale_date,
                            product=product,
                            defaults=defaults,
                        )
                        self.stats["SalesEvent"]["created" if created else "processed"] += 1
                    else:
                        self.stats["SalesEvent"]["processed"] += 1
                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["SalesEvent"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['SalesEvent']['processed']} processed, "
            f"{self.stats['SalesEvent']['skipped']} skipped, "
            f"{self.stats['SalesEvent']['errors']} errors\n"
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

                    if data.get("product") is not None and not self.write_disabled:
                        batch = self._get_pack_batch(
                            channel.id,
                            data["product"].id,
                            data["sale_date"],
                        )
                        if batch:
                            data["pack_batch"] = batch

                    if not self.write_disabled:
                        obj, created = NurseryEvent.objects.update_or_create(
                            planting=planting,
                            planned_date=data["planned_date"],
                            event_type=event_type,
                            defaults=data,
                        )
                        self.stats["NurseryEvent"]["created" if created else "processed"] += 1
                    else:
                        self.stats["NurseryEvent"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
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

                    if not self.write_disabled:
                        obj = HarvestEvent.objects.filter(
                            planting=planting, planned_date=planned_date
                        ).first()
                        created = obj is None
                        if created:
                            obj = HarvestEvent(planting=planting)
                        for k, v in data.items():
                            setattr(obj, k, v)
                        obj.save(skip_inventory_ledger_sync=True)
                        self.stats["HarvestEvent"]["created" if created else "processed"] += 1
                        # Cache harvest events for inventory lookups
                        cache_key = (planting_id, str(planned_date))
                        self.harvest_event_cache[cache_key] = obj
                    else:
                        self.stats["HarvestEvent"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
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
        """Import operations: field walk notes, planting field-record actuals, inventory, packs."""
        for year in range(self.start_year, self.end_year + 1):
            year_dir = os.path.join(self.data_dir, f"year_{year}")
            if not os.path.isdir(year_dir):
                continue

            self._import_field_walk_notes(year, year_dir)
            self._import_planting_field_records(year, year_dir)
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
                        planting = self._resolve_field_walk_planting_from_context(row, year)

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

                    if not self.write_disabled:
                        obj, created = FieldWalkNote.objects.update_or_create(
                            planting=planting,
                            walk_date=data["walk_date"],
                            defaults=data,
                        )
                        self.stats["FieldWalkNote"]["created" if created else "processed"] += 1
                    else:
                        self.stats["FieldWalkNote"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["FieldWalkNote"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['FieldWalkNote']['processed']} processed, "
            f"{self.stats['FieldWalkNote']['skipped']} skipped, "
            f"{self.stats['FieldWalkNote']['errors']} errors\n"
        )

    def _import_planting_field_records(self, year, year_dir):
        """Apply Field Records Online-style actuals to existing plantings.

        Source: year_YYYY/planting_field_records.csv — optional Stage A2 export from
        workbook 501 ``Field Records Online`` (columns A–F mapped here; plan identity G+).
        """
        path = os.path.join(year_dir, "planting_field_records.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing planting field records {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    if not self._planting_field_record_row_has_payload(row):
                        self.stats["PlantingFieldActuals"]["skipped"] += 1
                        continue

                    planting = self._resolve_planting_for_field_records(row, year)
                    if not planting:
                        self.stats["PlantingFieldActuals"]["skipped"] += 1
                        continue

                    updates = {}
                    date_str = (row.get("Actual Field Date") or "").strip()
                    if date_str:
                        updates["actual_plant_date"] = self._parse_date(date_str)

                    bedft_str = (row.get("Actual Bedft") or "").strip()
                    if bedft_str:
                        bf = self._int_or_none(bedft_str)
                        if bf is not None:
                            updates["actual_bedfeet"] = bf

                    fin = (row.get("Finished Harvesting") or "").strip().upper()
                    if fin in ("TRUE", "1", "YES", "Y"):
                        updates["status"] = PlantingStatus.COMPLETE

                    notes_add = (row.get("Notes") or "").strip()

                    if not self.write_disabled:
                        if notes_add:
                            prev = (planting.notes or "").strip()
                            planting.notes = f"{prev}\n{notes_add}".strip() if prev else notes_add

                        for field, value in updates.items():
                            setattr(planting, field, value)

                        save_fields = list(updates.keys())
                        if notes_add:
                            save_fields.append("notes")
                        planting.save(update_fields=sorted(set(save_fields)))

                        self.stats["PlantingFieldActuals"]["updated"] += 1
                    else:
                        self.stats["PlantingFieldActuals"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["PlantingFieldActuals"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['PlantingFieldActuals'].get('processed', 0)} processed, "
            f"{self.stats['PlantingFieldActuals'].get('updated', 0)} updated, "
            f"{self.stats['PlantingFieldActuals']['skipped']} skipped, "
            f"{self.stats['PlantingFieldActuals']['errors']} errors\n"
        )

    def _planting_field_record_row_has_payload(self, row):
        """True when the row carries at least one field-record field to apply."""
        date_str = (row.get("Actual Field Date") or "").strip()
        bedft_str = (row.get("Actual Bedft") or "").strip()
        notes_str = (row.get("Notes") or "").strip()
        fin = (row.get("Finished Harvesting") or "").strip().upper()
        finished_true = fin in ("TRUE", "1", "YES", "Y")
        return bool(date_str or bedft_str or notes_str or finished_true)

    def _resolve_planting_for_field_records(self, row, year):
        """Resolve Planting by Planting ID / ID cache, else same natural key as field_walk_notes."""
        planting_id = (row.get("Planting ID") or row.get("ID") or "").strip()
        planting = self._get_planting(planting_id) if planting_id else None
        if planting:
            return planting
        return self._resolve_field_walk_planting_from_context(row, year)

    def _resolve_field_walk_planting_from_context(self, row, year):
        """Resolve FieldWalkNote planting when Planting ID is missing.

        Policy: match a unique planting by (`Crop // Variety`, `Block`, `Bed`,
        `Plan Field Year`, `Plan Field Week`).
        """
        crop_variety = (row.get("Crop // Variety") or "").strip()
        block_name = (row.get("Block") or "").strip()
        bed_raw = (row.get("Bed") or "").strip()
        plan_year_raw = (row.get("Plan Field Year") or "").strip()
        plan_week_raw = (row.get("Plan Field Week") or "").strip()

        if not (crop_variety and block_name and bed_raw and plan_year_raw and plan_week_raw):
            return None

        crop_name, variety = self._split_crop_variety(crop_variety)
        if not crop_name:
            return None

        plan_year = self._int(plan_year_raw, 0)
        plan_week = self._int(plan_week_raw, 0)
        if plan_year <= 0 or plan_week <= 0:
            return None
        if plan_year != year:
            return None

        bed_number = self._int(bed_raw, 0)
        if bed_number <= 0:
            return None

        block = self._get_block(block_name)
        crop = self._get_crop(crop_name)
        if not block or not crop:
            return None

        candidates = (
            Planting.objects.filter(
                planning_year__year=plan_year,
                crop=crop,
                block=block,
                bed_start__lte=bed_number,
                bed_end__gte=bed_number,
            )
            .order_by("id")
        )

        if variety:
            candidates = candidates.filter(variety__iexact=variety)

        matches = []
        for planting in candidates:
            iso_year, iso_week, _ = planting.planned_plant_date.isocalendar()
            if iso_year == plan_year and iso_week == plan_week:
                matches.append(planting)

        if len(matches) == 1:
            return matches[0]
        return None

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

                    if not crop_name:
                        self._record_missing_required(
                            "InventoryLedger",
                            i,
                            "inventory_ledger.crop",
                            "Crop Name",
                        )
                        self.stats["InventoryLedger"]["skipped"] += 1
                        continue

                    if not crop:
                        self._record_stale_fk(
                            "InventoryLedger",
                            i,
                            "inventory_ledger.crop",
                            "crop",
                            crop_name,
                        )
                        self.stats["InventoryLedger"]["skipped"] += 1
                        continue

                    event_date_str = row.get("Event Date", "").strip()
                    if not event_date_str:
                        self._record_missing_required(
                            "InventoryLedger",
                            i,
                            "inventory_ledger.event_date",
                            "Event Date",
                        )
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

                    if not self.write_disabled:
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

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
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
                    recipe_name = (row.get("Recipe Name") or "").strip()
                    packed_quantity_raw = (row.get("Packed Quantity") or "").strip()
                    packed_unit = (row.get("Packed Unit") or "").strip()

                    if not (channel_name and product_name and pack_date_str):
                        if not channel_name:
                            self._record_missing_required(
                                "PackAllocation",
                                i,
                                "pack_allocations.channel",
                                "Channel",
                            )
                        if not product_name:
                            self._record_missing_required(
                                "PackAllocation",
                                i,
                                "pack_allocations.product",
                                "Product",
                            )
                        if not pack_date_str:
                            self._record_missing_required(
                                "PackAllocation",
                                i,
                                "pack_allocations.pack_date",
                                "Pack Date",
                            )
                        self.stats["PackAllocation"]["skipped"] += 1
                        continue

                    # Get FKs
                    channel = self._get_channel(channel_name)
                    product = self._get_product_by_name(product_name)

                    if not channel:
                        self._record_stale_fk(
                            "PackAllocation",
                            i,
                            "pack_allocations.channel",
                            "sales channel",
                            channel_name,
                        )
                        self.stats["PackAllocation"]["skipped"] += 1
                        continue
                    if not self._has_channel_rollup_assignment(
                        "PackAllocation",
                        i,
                        "pack_allocations.channel_rollup",
                        channel.name,
                    ):
                        self.stats["PackAllocation"]["skipped"] += 1
                        continue
                    if not product:
                        self._record_stale_fk(
                            "PackAllocation",
                            i,
                            "pack_allocations.product",
                            "product",
                            product_name,
                        )
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

                    pack_batch = None
                    if recipe_name:
                        recipe = self._get_recipe_for_product(product, recipe_name)
                        if not recipe:
                            self._record_stale_fk(
                                "PackAllocation",
                                i,
                                "pack_allocations.recipe",
                                "mix recipe",
                                recipe_name,
                            )
                            self.stats["PackAllocation"]["skipped"] += 1
                            continue
                        if (
                            getattr(recipe, "product_id", None) is not None
                            and getattr(product, "pk", None) is not None
                            and recipe.product_id != product.id
                        ):
                            self._record_row_error(
                                "PackAllocation",
                                i,
                                code="namespace_mismatch",
                                field_path="pack_allocations.recipe",
                                message=f"recipe '{recipe_name}' does not belong to product '{product_name}'",
                            )
                            self.stats["PackAllocation"]["errors"] += 1
                            continue
                        if not packed_quantity_raw:
                            self._record_missing_required(
                                "PackAllocation",
                                i,
                                "pack_allocations.packed_quantity",
                                "Packed Quantity",
                            )
                            self.stats["PackAllocation"]["skipped"] += 1
                            continue
                        packed_quantity = self._dec(packed_quantity_raw)
                        if packed_quantity <= 0:
                            self._record_row_error(
                                "PackAllocation",
                                i,
                                code="namespace_mismatch",
                                field_path="pack_allocations.packed_quantity",
                                message="packed quantity must be positive",
                            )
                            self.stats["PackAllocation"]["errors"] += 1
                            continue
                        if not packed_unit:
                            packed_unit = recipe.output_unit or product.sale_unit

                        if not self.write_disabled:
                            pack_batch, _ = PackBatch.objects.update_or_create(
                                product=product,
                                recipe=recipe,
                                pack_date=data["pack_date"],
                                defaults={
                                    "packed_quantity": packed_quantity,
                                    "packed_unit": packed_unit,
                                    "notes": f"Imported from pack_allocations row {i}",
                                },
                            )
                            if not self._materialize_pack_batch_components(
                                pack_batch, recipe, i
                            ):
                                self.stats["PackAllocation"]["errors"] += 1
                                continue
                            self.pack_batch_cache[
                                self._build_pack_batch_key(channel.id, product.id, data["pack_date"])
                            ] = pack_batch
                            data["pack_batch"] = pack_batch
                        else:
                            self.pack_batch_cache[
                                self._build_pack_batch_key(channel.id, product.id, data["pack_date"])
                            ] = {
                                "product_id": product.id,
                                "pack_date": data["pack_date"],
                            }

                    if not self.write_disabled:
                        obj, created = PackAllocation.objects.update_or_create(
                            channel=channel,
                            product=product,
                            pack_date=data["pack_date"],
                            defaults=data,
                        )
                        self.stats["PackAllocation"]["created" if created else "processed"] += 1
                    else:
                        self.stats["PackAllocation"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
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
                        if not channel_name:
                            self._record_missing_required(
                                "SalesEvent",
                                i,
                                "sales_events.channel",
                                "Channel Name",
                            )
                        if not sale_date_str:
                            self._record_missing_required(
                                "SalesEvent",
                                i,
                                "sales_events.sale_date",
                                "Sale Date",
                            )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    channel = self._get_channel(channel_name)
                    if not channel:
                        self._record_stale_fk(
                            "SalesEvent",
                            i,
                            "sales_events.channel",
                            "sales channel",
                            channel_name,
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not self._has_channel_rollup_assignment(
                        "SalesEvent",
                        i,
                        "sales_events.channel_rollup",
                        channel.name,
                    ):
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
                        if not product:
                            self._record_stale_fk(
                                "SalesEvent",
                                i,
                                "sales_events.product",
                                "product",
                                product_name,
                            )
                            self.stats["SalesEvent"]["skipped"] += 1
                            continue
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
                    data["entry_kind"] = SalesEvent.EntryKind.ACTUAL

                    product_obj = data.get("product")
                    if product_obj and not self.write_disabled:
                        batch = self._get_pack_batch(channel.id, product_obj.id, data["sale_date"])
                        if batch:
                            data["pack_batch"] = batch

                    if not self.write_disabled:
                        obj = SalesEvent.objects.filter(
                            entry_kind=SalesEvent.EntryKind.ACTUAL,
                            channel=channel,
                            sale_date=data["sale_date"],
                            product=product_obj,
                        ).first()
                        created = obj is None
                        if created:
                            obj = SalesEvent(
                                entry_kind=SalesEvent.EntryKind.ACTUAL,
                                channel=channel,
                                sale_date=data["sale_date"],
                                product=product_obj,
                            )
                        for k, v in data.items():
                            setattr(obj, k, v)
                        obj.save(skip_inventory_ledger_sync=True)
                        self.stats["SalesEvent"]["created" if created else "processed"] += 1
                    else:
                        self.stats["SalesEvent"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
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
                        if not channel_name:
                            self._record_missing_required(
                                "QuickSalesEntry",
                                i,
                                "quick_sales_entries.channel",
                                "Channel Name",
                            )
                        if not sale_date_str:
                            self._record_missing_required(
                                "QuickSalesEntry",
                                i,
                                "quick_sales_entries.sale_date",
                                "Sale Date",
                            )
                        self.stats["QuickSalesEntry"]["skipped"] += 1
                        continue

                    channel = self._get_channel(channel_name)
                    if not channel:
                        self._record_stale_fk(
                            "QuickSalesEntry",
                            i,
                            "quick_sales_entries.channel",
                            "sales channel",
                            channel_name,
                        )
                        self.stats["QuickSalesEntry"]["skipped"] += 1
                        continue
                    if not self._has_channel_rollup_assignment(
                        "QuickSalesEntry",
                        i,
                        "quick_sales_entries.channel_rollup",
                        channel.name,
                    ):
                        self.stats["QuickSalesEntry"]["skipped"] += 1
                        continue

                    data = {
                        "channel": channel,
                        "sale_date": self._parse_date(sale_date_str),
                        "total_cash": self._dec(row.get("Total Cash", 0)),
                        "total_card": self._dec(row.get("Total Card", 0)),
                        "notes": row.get("Notes", "").strip(),
                    }

                    if not self.write_disabled:
                        obj, created = QuickSalesEntry.objects.update_or_create(
                            channel=channel,
                            sale_date=data["sale_date"],
                            defaults=data,
                        )
                        self.stats["QuickSalesEntry"]["created" if created else "processed"] += 1
                    else:
                        self.stats["QuickSalesEntry"]["processed"] += 1

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
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
        if not self.write_disabled:
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
            self.crop_cache[crop_name] = self._resolve_fk_by_text(
                CropInfo,
                "name",
                crop_name,
                label="crop",
            )
        return self.crop_cache[crop_name]

    def _get_block(self, block_name):
        """Get or cache block by name."""
        if block_name not in self.block_cache:
            self.block_cache[block_name] = self._resolve_fk_by_text(
                Block,
                "name",
                block_name,
                label="block",
            )
        return self.block_cache[block_name]

    def _get_channel(self, channel_name):
        """Get or cache sales channel by name."""
        if channel_name not in self.channel_cache:
            lookup_name = channel_name
            if channel_name:
                alias_key = self._normalize_lookup_value(channel_name)
                if alias_key in self.channel_name_aliases:
                    lookup_name = self.channel_name_aliases[alias_key]
            self.channel_cache[channel_name] = self._resolve_fk_by_text(
                SalesChannel,
                "name",
                lookup_name,
                label="sales channel",
            )
        return self.channel_cache[channel_name]

    def _get_product_by_name(self, product_name):
        """Get crop sales format by product name."""
        if product_name in self.product_cache:
            return self.product_cache[product_name]
        resolved = self._resolve_fk_by_text(
            CropSalesFormat,
            "product_name",
            product_name,
            label="product",
        )
        self.product_cache[product_name] = resolved
        return resolved

    def _get_recipe_for_product(self, product, recipe_name):
        """Resolve ProductRecipe for this product and recipe name (scoped, not global)."""
        if not recipe_name:
            return None
        product_pk = getattr(product, "pk", None)
        if product_pk:
            cache_key = (product_pk, recipe_name)
            if cache_key in self.recipe_cache:
                return self.recipe_cache[cache_key]
            resolved = (
                ProductRecipe.objects.filter(product=product, name=recipe_name)
                .order_by("id")
                .first()
            )
            self.recipe_cache[cache_key] = resolved
            return resolved
        if recipe_name in self.recipe_cache:
            return self.recipe_cache[recipe_name]
        resolved = self._resolve_fk_by_text(
            ProductRecipe,
            "name",
            recipe_name,
            label="mix recipe",
        )
        self.recipe_cache[recipe_name] = resolved
        return resolved

    def _build_pack_batch_key(self, channel_id, product_id, pack_date):
        return f"{channel_id}:{product_id}:{pack_date.isoformat()}"

    def _get_pack_batch(self, channel_id, product_id, pack_date):
        key = self._build_pack_batch_key(channel_id, product_id, pack_date)
        return self.pack_batch_cache.get(key)

    def _get_crop_season(self, crop, block_type):
        """Resolve crop season with deterministic duplicate handling."""
        if self.write_disabled:
            return {"crop": getattr(crop, "id", crop), "block_type": block_type}
        queryset = CropBySeason.objects.filter(crop=crop, block_type=block_type).order_by("id")
        crop_season = queryset.first()
        if crop_season and queryset.count() > 1:
            self.stdout.write(
                self.style.WARNING(
                    f"   ⚠  Multiple crop_season matches for {crop}/{block_type}; using id={crop_season.id}"
                )
            )
        return crop_season

    def _normalize_lookup_value(self, raw_value):
        """Normalize lookup text by trimming, collapsing spaces, and casefolding."""
        if raw_value is None:
            return ""
        return " ".join(str(raw_value).strip().split()).casefold()

    def _build_normalized_lookup_index(self, model, field_name):
        """Build cached normalized lookup index for a model field."""
        cache_key = f"{model._meta.label_lower}:{field_name}"
        if cache_key in self.normalized_lookup_indexes:
            return self.normalized_lookup_indexes[cache_key]

        normalized_index = defaultdict(list)
        for obj in model.objects.all().only("id", field_name).order_by("id"):
            normalized_index[self._normalize_lookup_value(getattr(obj, field_name))].append(obj.id)
        self.normalized_lookup_indexes[cache_key] = normalized_index
        return normalized_index

    def _resolve_fk_by_text(self, model, field_name, raw_value, label):
        """Resolve FK using exact match first, normalized fallback second."""
        if self.write_disabled:
            return raw_value
        if not raw_value:
            return None

        exact_value = str(raw_value).strip()
        exact_matches = model.objects.filter(**{field_name: exact_value}).order_by("id")
        first_exact = exact_matches.first()
        if first_exact:
            if exact_matches.count() > 1:
                self.stdout.write(
                    self.style.WARNING(
                        f"   ⚠  Multiple {label} matches for '{raw_value}'; using id={first_exact.id}"
                    )
                )
            return first_exact

        normalized_value = self._normalize_lookup_value(raw_value)
        if not normalized_value:
            return None
        normalized_index = self._build_normalized_lookup_index(model, field_name)
        candidate_ids = normalized_index.get(normalized_value, [])
        if not candidate_ids:
            return None

        candidate = model.objects.filter(id=candidate_ids[0]).first()
        if candidate and len(candidate_ids) > 1:
            self.stdout.write(
                self.style.WARNING(
                    f"   ⚠  Multiple normalized {label} matches for '{raw_value}'; using id={candidate.id}"
                )
            )
        return candidate

    def _get_planning_year(self, year):
        """Get or cache planning year."""
        if year not in self.planning_year_cache:
            try:
                if not self.write_disabled:
                    self.planning_year_cache[year] = PlanningYear.objects.get(year=year)
                else:
                    self.planning_year_cache[year] = year
            except PlanningYear.DoesNotExist:
                self.planning_year_cache[year] = None
        return self.planning_year_cache[year]

    def _get_planting(self, planting_id):
        """Get planting from cache."""
        return self.planting_cache.get(planting_id)

    def _split_crop_variety(self, crop_variety):
        """Split 'Crop // Variety' text into normalized crop and variety."""
        if "//" not in crop_variety:
            return crop_variety.strip(), ""
        left, right = crop_variety.split("//", 1)
        return left.strip(), right.strip()

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

        total_created = 0
        total_updated = 0
        total_skipped = 0
        total_error = 0

        for model_name in sorted(self.stats.keys()):
            normalized = self._normalized_outcomes(self.stats[model_name])
            created = normalized["created"]
            updated = normalized["updated"]
            skipped = normalized["skipped"]
            error = normalized["error"]

            total_created += created
            total_updated += updated
            total_skipped += skipped
            total_error += error

            status = "✓" if error == 0 else "⚠"
            self.stdout.write(
                f"  {status} {model_name:25} created={created:3} updated={updated:3} "
                f"skipped={skipped:3} error={error:3}"
            )

        self.stdout.write(
            f"\n  TOTALS: created={total_created:3} updated={total_updated:3} "
            f"skipped={total_skipped:3} error={total_error:3}\n"
        )
        if total_error > 0:
            # Print deterministic owner/escalation routing so operators can triage
            # directly from terminal output without opening JSON artifacts first.
            failure_signatures = self._build_failure_signatures(status="ok", fatal_error=None)
            escalation_summary = self._build_escalation_summary(failure_signatures)
            self._print_escalation_handoff(escalation_summary)

        if self.validate_only:
            self.stdout.write(self.style.WARNING("\n⚠️  VALIDATE-ONLY/PREFLIGHT — no data saved"))
        elif self.dry_run:
            self.stdout.write(self.style.WARNING("\n⚠️  DRY-RUN — no data saved"))
        else:
            self.stdout.write(self.style.SUCCESS("\n✓ All data saved successfully"))

    def _print_escalation_handoff(self, escalation_summary):
        """Emit operator-facing escalation buckets for rapid incident routing."""
        self.stdout.write("\n🚨 ESCALATION HANDOFF")
        for bucket in escalation_summary:
            signatures = ",".join(bucket["signatures"])
            recovery_steps = " || ".join(bucket["recovery_steps"])
            self.stdout.write(
                "  - "
                f"{bucket['severity']} | {bucket['owner_area']} | {bucket['owner_team']} | "
                f"{bucket['escalation_path']} | count={bucket['count']} | signatures={signatures} | "
                f"recovery={recovery_steps}"
            )

    def _format_fatal_error(self, exc):
        """Build deterministic fatal error details for recovery handoff."""
        mode = "validate-only" if self.validate_only else "apply"
        return (
            f"{exc.__class__.__name__}: {exc} "
            f"[mode={mode}, atomic_apply={self.atomic_apply}, dry_run={self.dry_run}]"
        )

    def _normalized_outcomes(self, model_stats):
        """Normalize legacy counters to canonical outcomes."""
        created = model_stats.get("created", 0)
        skipped = model_stats.get("skipped", 0)
        error = model_stats.get("error", 0) + model_stats.get("errors", 0)
        updated = model_stats.get("updated", 0) + model_stats.get("processed", 0)

        if self.dry_run or self.validate_only:
            # Write-disabled modes report would-be writes as skipped.
            skipped += created + updated
            created = 0
            updated = 0

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "error": error,
        }

    def _build_failure_signatures(self, status, fatal_error):
        """Aggregate deterministic failure signatures with ownership mapping."""
        signature_counts = defaultdict(int)
        signature_examples = {}
        for item in self.row_errors:
            signature = item.get("code") or "unknown"
            signature_counts[signature] += 1
            signature_examples.setdefault(
                signature,
                {
                    "model": item.get("model"),
                    "field_path": item.get("field_path"),
                    "message": item.get("message"),
                },
            )

        if status == "failed" and fatal_error:
            signature_counts["fatal_import_exception"] += 1
            signature_examples.setdefault(
                "fatal_import_exception",
                {"model": "ImportRun", "field_path": "run", "message": str(fatal_error)},
            )

        signatures = []
        for signature in sorted(signature_counts.keys()):
            ownership = self.FAILURE_SIGNATURE_OWNERSHIP.get(
                signature, self.FAILURE_SIGNATURE_OWNERSHIP["unknown"]
            )
            signatures.append(
                {
                    "signature": signature,
                    "count": signature_counts[signature],
                    "owner_area": ownership["owner_area"],
                    "owner_team": ownership["owner_team"],
                    "severity": ownership["severity"],
                    "escalation_path": ownership["escalation_path"],
                    "recovery": ownership["recovery"],
                    "example": signature_examples[signature],
                }
            )
        return signatures

    def _build_escalation_summary(self, failure_signatures):
        """Group failure signatures into operator-facing escalation buckets."""
        grouped = {}
        for item in failure_signatures:
            key = (
                item["owner_area"],
                item["owner_team"],
                item["severity"],
                item["escalation_path"],
            )
            if key not in grouped:
                grouped[key] = {
                    "owner_area": item["owner_area"],
                    "owner_team": item["owner_team"],
                    "severity": item["severity"],
                    "escalation_path": item["escalation_path"],
                    "count": 0,
                    "signatures": [],
                    "recovery_steps": [],
                }
            grouped[key]["count"] += item["count"]
            grouped[key]["signatures"].append(item["signature"])
            grouped[key]["recovery_steps"].append(item["recovery"])

        escalation_summary = sorted(
            grouped.values(),
            key=lambda row: (
                row["severity"],
                row["owner_area"],
                row["owner_team"],
                row["escalation_path"],
            ),
        )
        for row in escalation_summary:
            row["signatures"].sort()
            # Keep deterministic unique recovery hints for operator handoff.
            row["recovery_steps"] = sorted(set(row["recovery_steps"]))
        return escalation_summary

    def _write_summary_json(self, status="ok", fatal_error=None):
        """Write structured summary artifact when requested."""
        per_model = {}
        totals = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
        for model_name in sorted(self.stats.keys()):
            normalized = self._normalized_outcomes(self.stats[model_name])
            per_model[model_name] = normalized
            for key in totals:
                totals[key] += normalized[key]

        failure_signatures = self._build_failure_signatures(status, fatal_error)
        payload = {
            "schema_version": "1.3",
            "status": status,
            "fatal_error": fatal_error,
            "run": {
                "started_at": self.run_started_at.isoformat() + "Z",
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "run_id": self.run_id,
                "data_dir": self.data_dir,
                "start_year": self.start_year,
                "end_year": self.end_year,
                "validate_only": self.validate_only,
                "dry_run": self.dry_run,
                "atomic_apply": self.atomic_apply,
                "verbose": self.verbose,
            },
            "results": {
                "models": per_model,
                "totals": totals,
                "row_errors": self.row_errors,
                "failure_signatures": failure_signatures,
                "escalation_summary": self._build_escalation_summary(failure_signatures),
            },
        }

        output_dir = os.path.dirname(os.path.abspath(self.summary_json_path))
        try:
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            with open(self.summary_json_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
        except OSError as exc:
            fallback_path = Path(self.data_dir) / "_import_artifacts" / f"historical-import-summary-{self.run_id}.json"
            self.stderr.write(
                self.style.WARNING(
                    f"⚠ unable to write summary artifact at '{self.summary_json_path}' ({exc}); "
                    f"falling back to '{fallback_path}'"
                )
            )
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with open(fallback_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            self.summary_json_path = str(fallback_path)
