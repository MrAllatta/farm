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
import re
import sys
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction, DatabaseError, IntegrityError

from reference.models import (
    CropInfo,
    Variety,
    Block,
    CropBySeason,
    CropSalesFormat,
    CropSalesFormatYear,
    ProductRecipe,
    ProductRecipeComponent,
    SalesCategory,
    SalesChannel,
    SalesPlanBucket,
)
from reference.sales_rollups import ANNUAL_PLAN_SALES_CHANNELS
from reference.services.crop_category import derive_fresh_or_storage
from planning.models import (
    PlanningYear,
    Planting,
    SeedOrder,
    NurseryEvent,
    HarvestEvent,
    PlantingStatus,
)
from operations.models import (
    FieldWalkNote,
    InventoryLedger,
    PackAllocation,
    PackBatch,
    PackBatchComponent,
)
from sales.models import SalesEvent, QuickSalesEntry
from core.models import RotationHistory
from planning.services import nursery_plan_sheet

# Reference-tier ``product_recipe_components.csv`` rows without ``Planning Year`` bucket here.
DEFAULT_PRODUCT_RECIPE_PLANNING_YEAR = 2026


def compose_crop_sales_format_product_name(crop_name: str, format_column: str) -> str:
    """Derive ``CropSalesFormat.product_name`` from Farm Crop Formats columns.

    The workbook maps **Product** → crop label and **Format** → ``Product Name`` in CSV.
    When Format is a suffix such as ``Persian - lb`` (does not start with the crop token),
    concatenate so Sales Plan 302 strings like ``Cucumber Persian - lb`` resolve after reference
    import. When Format already includes the crop (``Cucumber - lb``), return it unchanged.
    """
    crop_name = (crop_name or "").strip()
    format_column = (format_column or "").strip()
    if not format_column:
        return ""
    cn = crop_name.casefold()
    pn = format_column.casefold()
    if not cn:
        return format_column
    if pn == cn:
        return format_column
    if pn.startswith(cn):
        remainder = pn[len(cn) :]
        if not remainder or remainder[0] in " -/|":
            return format_column
    return f"{crop_name} {format_column}".strip()


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
    PLANTING_PROGRESS_EVERY = 100

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
        ref = parser.add_mutually_exclusive_group(required=False)
        ref.add_argument(
            "--require-reference",
            dest="require_reference",
            action="store_true",
            default=None,
            help="Require Tier-1 reference CSVs when their target tables are empty (default: on in apply mode)",
        )
        ref.add_argument(
            "--no-require-reference",
            dest="require_reference",
            action="store_false",
            default=None,
            help="Allow apply without Tier-1 reference CSVs when target tables are empty (unsafe for fresh DBs)",
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
        # Single strict-apply switch: revert importer strictness by restoring the pre-strict assignment below.
        self.strict_apply = not self.validate_only and not self.dry_run
        if options.get("require_reference") is None:
            self.require_reference = self.strict_apply
        else:
            self.require_reference = bool(options["require_reference"])
        requested_summary_path = options.get("summary_json")
        self.run_started_at = datetime.utcnow()
        # Use microsecond precision to avoid artifact path collisions on rapid retries.
        self.run_id = self.run_started_at.strftime("%Y%m%dT%H%M%S%f")
        self.summary_json_path = self._resolve_summary_json_path(requested_summary_path)
        self.row_errors = []
        self.row_warnings: list[dict] = []
        self.skip_reasons: list[dict] = []

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
        self.product_name_aliases = {}
        self.channel_rollup_map = {}
        self.channel_rollup_required = False
        self.product_cache = {}
        self.recipe_cache = {}
        self.planning_year_cache = {}
        self.planting_cache = {}
        self.crop_season_cache = {}
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
            totals = self._aggregate_import_totals()
            apply_row_errors_failed = self.strict_apply and totals["error"] > 0
            summary_status = "failed" if apply_row_errors_failed else "ok"
            self._write_summary_json(status=summary_status)
            self.stdout.write("=" * 70 + "\n")
            if apply_row_errors_failed:
                raise CommandError(
                    f"apply mode finished with totals.error={totals['error']} (see '{self.summary_json_path}')"
                )

        except CommandError:
            raise
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
        self._enforce_reference_tier_csv_presence()
        self._load_channel_name_aliases()
        self._load_product_name_aliases()
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

        # After all tiers: ensure plantings have generated harvest/nursery rows when still missing
        # (import CSVs may omit harvest_events.csv / nursery_events.csv for some years).
        if not self.write_disabled and not self.validate_only:
            from planning.services.planting_events_repair import repair_planting_events

            rep = repair_planting_events(min_year=self.start_year, max_year=self.end_year)
            self.stdout.write(
                self.style.SUCCESS(
                    "\nRepair generated planting events: "
                    f"scanned={rep.plantings_scanned} "
                    f"harvest_backfill_plantings={rep.harvest_events_created_plantings} "
                    f"nursery_backfill_plantings={rep.nursery_events_created_plantings} "
                    f"invalid_harvest_window={rep.harvest_skipped_invalid_window}\n"
                )
            )

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

    def _record_row_warning(self, model_name, row_number, code, field_path, message):
        """Capture structured row-level warning details for summary artifacts."""
        self.row_warnings.append(
            {
                "model": model_name,
                "row": row_number,
                "code": code,
                "field_path": field_path,
                "message": str(message),
            }
        )

    def _record_skip_reason(self, model_name, row_number, code, field_path, message):
        """Capture explicit row-level skip reason details for summary artifacts."""
        self.skip_reasons.append(
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

    def _record_planting_date_year_mismatch(
        self, folder_year: int, row_number: int, plant_date: date
    ) -> None:
        """LIVE-8: Planned Plant Date calendar year is far from the year_NNNN bundle directory.

        Does not block import; surfaces in preflight/validate and summary row_warnings.
        """
        message = (
            f"Planned Plant Date {plant_date.isoformat()} is more than one year away from "
            f"bundle folder year {folder_year} — confirm this row belongs in year_{folder_year} "
            f"(import binds PlanningYear to the folder year; missing-plantings and ops views can "
            f"show unexpected calendar years if the CSV is misplaced)."
        )
        self.stdout.write(self.style.WARNING(f"    ⚠  row {row_number}: {message}"))
        self._record_row_warning(
            "Planting",
            row_number,
            "planting_date_year_mismatch",
            "plantings.planned_plant_date",
            message,
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
        self._import_seed_sources()
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

    def _load_product_name_aliases(self):
        """Optional reference/product_name_aliases.csv: map imported product labels to CropSalesFormat.product_name."""
        path = self._resolve_reference_path("product_name_aliases.csv")
        if not os.path.exists(path):
            return
        self.stdout.write("Loading product name aliases...")
        loaded = 0
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                alias = (row.get("Alias") or row.get("alias") or "").strip()
                canonical = (
                    (row.get("Product Name") or row.get("product_name") or row.get("Canonical") or "")
                    .strip()
                )
                if not alias or not canonical:
                    continue
                key = self._normalize_lookup_value(alias)
                if key in self.product_name_aliases and self.product_name_aliases[key] != canonical:
                    raise CommandError(
                        f"product_name_aliases.csv row {i}: conflicting canonical product for alias '{alias}'"
                    )
                self.product_name_aliases[key] = canonical
                loaded += 1
        self.stdout.write(f"  loaded {loaded} product name aliases\n")

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
                channel_obj, _ = self._upsert_sales_channel_by_name(
                    channel_name,
                    channel_defaults,
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
            channel_obj, created_flag = self._upsert_sales_channel_by_name(
                channel_name,
                channel_defaults,
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

        # Match crop_by_season: keys are casefolded, collapsed whitespace (sheet / API casing varies).
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

                    block_type_raw = row["Block Type"].strip()
                    normalized_block_type = " ".join(block_type_raw.split()).casefold()
                    block_type = type_map.get(normalized_block_type, "field")

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

                    storage_weeks = self._int(row.get("Storage Weeks", 0))
                    can_hold_in_field = self._bool_csv(row.get("Can Hold In Field"))
                    fresh_or_storage = derive_fresh_or_storage(
                        storage_weeks=storage_weeks,
                        can_hold_in_field=can_hold_in_field,
                    )

                    data = {
                        "crop_type": crop_type,
                        "botanical_family": botanical_family,
                        "propagation_type": propagation_type,
                        "is_perennial": is_perennial,
                        "fresh_or_storage": fresh_or_storage,
                        "storage_weeks": storage_weeks,
                        "can_hold_in_field": can_hold_in_field,
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
                        obj, created = self._upsert_sales_channel_by_name(name, data)
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
                    raw_product_name = row.get("Product Name", "").strip()
                    product_name = compose_crop_sales_format_product_name(crop_name, raw_product_name)

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

                    plan_y_raw = (row.get("Planning Year") or "").strip()
                    stable_defaults = {
                        "sale_unit": row.get("Sale Unit", "").strip() or "pound",
                        "harvest_qty_per_sale_unit": self._dec(
                            row.get("Harvest Qty Per Sale Unit", 1)
                        ),
                        "sku": row.get("SKU", "").strip(),
                    }
                    yearly_price = self._dec(row.get("Sale Price", 0))
                    yearly_active = row.get("Is Active", "true").strip().lower() == "true"

                    if not self.write_disabled:
                        if plan_y_raw:
                            plan_year_int = self._int(plan_y_raw)
                            obj, created = CropSalesFormat.objects.update_or_create(
                                crop=crop, product_name=product_name, defaults=stable_defaults
                            )
                            py = self._ensure_planning_year(plan_year_int)
                            CropSalesFormatYear.objects.update_or_create(
                                product=obj,
                                planning_year=py,
                                defaults={
                                    "sale_price": yearly_price,
                                    "is_active": yearly_active,
                                },
                            )
                            obj.refresh_sale_cache_from_yearly()
                        else:
                            # Legacy reference CSV (no Planning Year): keep price on CropSalesFormat only
                            # so we do not create PlanningYear rows from command default --end-year.
                            legacy_defaults = {
                                **stable_defaults,
                                "sale_price": yearly_price,
                                "is_active": yearly_active,
                            }
                            obj, created = CropSalesFormat.objects.update_or_create(
                                crop=crop, product_name=product_name, defaults=legacy_defaults
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
            py_raw = (row.get("Planning Year") or "").strip()
            bucket_year = self._int(py_raw) if py_raw else DEFAULT_PRODUCT_RECIPE_PLANNING_YEAR
            buckets[(bucket_year, csf.id)].append((i, row))
            csf_by_id[csf.id] = csf

        for (_bucket_year, pk), rows_group in buckets.items():
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
            planning_year_obj = (
                self._ensure_planning_year(bucket_year) if not self.write_disabled else None
            )
            self._prc_process_recipe_group(ordered, csf, recipe_final, planning_year_obj)

        rp = self.stats["ProductRecipe"]
        rc = self.stats["ProductRecipeComponent"]
        self.stdout.write(
            f" {rp.get('processed', 0) + rp.get('created', 0)} recipes, "
            f"{rc.get('processed', 0) + rc.get('created', 0)} components, "
            f"{rp.get('errors', 0)} recipe errors, "
            f"{rc.get('errors', 0)} component errors\n"
        )

    def _import_seed_sources(self):
        """Import reference/seed_sources.csv into Variety."""
        path = self._resolve_reference_path("seed_sources.csv")
        if not os.path.exists(path):
            self.stdout.write("  ⊘ seed_sources.csv not found\n")
            return

        self.stdout.write("Importing seed sources...")
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                crop_name = (row.get("Crop") or "").strip()
                variety_name = (row.get("Variety") or "").strip()
                if not crop_name and not variety_name:
                    continue
                if not crop_name:
                    self._record_missing_required("Variety", i, "seed_sources.crop", "Crop")
                    self.stats["Variety"]["errors"] += 1
                    continue
                if not variety_name:
                    self._record_missing_required("Variety", i, "seed_sources.variety", "Variety")
                    self.stats["Variety"]["errors"] += 1
                    continue

                crop = self._get_crop(crop_name)
                if not crop:
                    self._record_stale_fk("Variety", i, "seed_sources.crop", "crop", crop_name)
                    self.stats["Variety"]["skipped"] += 1
                    continue

                defaults = {
                    "supplier": (row.get("Supplier") or "").strip(),
                    "catalog_number": (row.get("Catalog Number") or row.get("Catalog") or "").strip(),
                    "source_url": (row.get("Source URL") or row.get("URL") or "").strip()[:500],
                    "notes": (row.get("Notes") or "").strip(),
                }
                try:
                    if not self.write_disabled:
                        _, created = Variety.objects.update_or_create(
                            crop=crop, name=variety_name, defaults=defaults
                        )
                        self.stats["Variety"]["created" if created else "processed"] += 1
                    else:
                        self.stats["Variety"]["processed"] += 1
                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["Variety"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['Variety']['processed']} processed, "
            f"{self.stats['Variety']['skipped']} skipped, "
            f"{self.stats['Variety']['errors']} errors\n"
        )

    def _import_seed_orders(self):
        """Import reference/seed_orders.csv into SeedOrder."""
        path = self._resolve_reference_path("seed_orders.csv")
        if not os.path.exists(path):
            self.stdout.write("  ⊘ seed_orders.csv not found\n")
            return

        self.stdout.write("Importing seed orders...")
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                crop_name = (row.get("Crop") or "").strip()
                variety_name = (row.get("Variety") or "").strip()
                year_raw = (row.get("Season Year") or "").strip()
                qty_raw = (row.get("Planned Quantity") or "").strip()
                unit = (row.get("Unit") or "ounces").strip()[:20]
                notes = (row.get("Notes") or "").strip()

                if not any([crop_name, variety_name, year_raw, qty_raw, unit, notes]):
                    continue
                if not crop_name:
                    self._record_missing_required("SeedOrder", i, "seed_orders.crop", "Crop")
                    self.stats["SeedOrder"]["errors"] += 1
                    continue
                if not variety_name:
                    self._record_missing_required("SeedOrder", i, "seed_orders.variety", "Variety")
                    self.stats["SeedOrder"]["errors"] += 1
                    continue
                if not year_raw:
                    self._record_missing_required(
                        "SeedOrder", i, "seed_orders.season_year", "Season Year"
                    )
                    self.stats["SeedOrder"]["errors"] += 1
                    continue
                if not qty_raw:
                    self._record_missing_required(
                        "SeedOrder",
                        i,
                        "seed_orders.planned_quantity",
                        "Planned Quantity",
                    )
                    self.stats["SeedOrder"]["errors"] += 1
                    continue

                try:
                    year = self._int(year_raw)
                except (ValueError, TypeError):
                    self._record_row_error(
                        "SeedOrder",
                        i,
                        code="namespace_mismatch",
                        field_path="seed_orders.season_year",
                        message=f"invalid Season Year '{year_raw}'",
                    )
                    self.stats["SeedOrder"]["errors"] += 1
                    continue

                planning_year = self._get_planning_year(year)
                if not planning_year:
                    self._record_stale_fk(
                        "SeedOrder",
                        i,
                        "seed_orders.planning_year",
                        "planning year",
                        str(year),
                    )
                    self.stats["SeedOrder"]["skipped"] += 1
                    continue

                crop = self._get_crop(crop_name)
                if not crop:
                    self._record_stale_fk("SeedOrder", i, "seed_orders.crop", "crop", crop_name)
                    self.stats["SeedOrder"]["skipped"] += 1
                    continue

                try:
                    qty = self._dec(qty_raw)
                except (InvalidOperation, ValueError, TypeError):
                    self._record_row_error(
                        "SeedOrder",
                        i,
                        code="namespace_mismatch",
                        field_path="seed_orders.planned_quantity",
                        message=f"invalid Planned Quantity '{qty_raw}'",
                    )
                    self.stats["SeedOrder"]["errors"] += 1
                    continue

                if qty <= 0:
                    self.stats["SeedOrder"]["skipped"] += 1
                    continue

                variety = Variety.objects.filter(crop=crop, name=variety_name).order_by("id").first()
                if not variety:
                    vdefaults = {
                        "supplier": (row.get("Supplier") or "").strip(),
                        "catalog_number": (row.get("Catalog Number") or row.get("Catalog") or "").strip(),
                        "source_url": (row.get("Source URL") or row.get("URL") or "").strip()[:500],
                        "notes": (row.get("Variety Notes") or "").strip(),
                    }
                    try:
                        if not self.write_disabled:
                            variety, vcreated = Variety.objects.update_or_create(
                                crop=crop, name=variety_name, defaults=vdefaults
                            )
                            self.stats["Variety"]["created" if vcreated else "processed"] += 1
                        else:
                            self.stats["Variety"]["processed"] += 1
                            variety = None
                    except (
                        ValueError,
                        KeyError,
                        InvalidOperation,
                        ValidationError,
                        IntegrityError,
                        DatabaseError,
                    ) as e:
                        self.stderr.write(f"    ERROR row {i}: {e}")
                        self.stats["Variety"]["errors"] += 1
                        self.stats["SeedOrder"]["errors"] += 1
                        continue

                defaults = {
                    "planned_quantity": qty,
                    "unit": unit,
                    "notes": notes,
                }
                try:
                    if not self.write_disabled:
                        _, created = SeedOrder.objects.update_or_create(
                            variety=variety,
                            planning_year=planning_year,
                            defaults=defaults,
                        )
                        self.stats["SeedOrder"]["created" if created else "processed"] += 1
                    else:
                        self.stats["SeedOrder"]["processed"] += 1
                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR row {i}: {e}")
                    self.stats["SeedOrder"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['SeedOrder']['processed']} processed, "
            f"{self.stats['SeedOrder']['skipped']} skipped, "
            f"{self.stats['SeedOrder']['errors']} errors\n"
        )

    def _prc_sort_key(self, row, row_num):
        lo = self._prc_field(row, "Line Order")
        if lo:
            try:
                return (0, int(lo))
            except ValueError:
                pass
        return (1, row_num)

    def _prc_process_recipe_group(self, ordered_pairs, csf, recipe_final, planning_year):
        """Create or replace one ProductRecipe and its ProductRecipeComponents."""
        model_rc = "ProductRecipeComponent"
        if self.write_disabled:
            self.stats["ProductRecipe"]["processed"] += 1
            self.stats["ProductRecipeComponent"]["processed"] += len(ordered_pairs)
            return
        recipe_final = recipe_final or "Default"
        ProductRecipe.objects.filter(product=csf, planning_year=planning_year).update(
            is_active=False
        )
        recipe, created = ProductRecipe.objects.update_or_create(
            product=csf,
            name=recipe_final,
            planning_year=planning_year,
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
        for rec in ProductRecipe.objects.select_related("product", "planning_year").iterator():
            py_year = rec.planning_year.year if rec.planning_year_id else 0
            cache_key = (rec.product_id, py_year, rec.name)
            self.recipe_cache[cache_key] = rec

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
            if (Path(year_dir) / "sales_plan_302.csv").exists():
                self._import_sales_plan_302(year, year_dir)
            else:
                self._import_product_week_plan(year, year_dir)
            self._import_plantings(year, year_dir)
        self._import_seed_orders()

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

    @staticmethod
    def _normalize_planting_variety_text(raw: str) -> str:
        return " ".join((raw or "").replace("\n", " ").split()).strip()

    def _planting_variety_merge_key(self, raw: str) -> str:
        return self._normalize_planting_variety_text(raw).casefold()

    def _resolve_planting_variety_fk(self, crop, variety_text: str):
        """Resolve or create ``Variety`` for a planting row; returns FK or None."""
        norm = self._normalize_planting_variety_text(variety_text)
        if not norm:
            return None
        existing = Variety.objects.filter(crop=crop, name__iexact=norm).order_by("id").first()
        if existing:
            return existing
        if self.write_disabled:
            return None
        v, _ = Variety.objects.get_or_create(
            crop=crop,
            name=norm,
            defaults={
                "supplier": "",
                "catalog_number": "",
                "notes": "",
            },
        )
        return v

    def _parse_single_planting_csv_row(self, year, i, row, status_map):
        """Return a dict describing one CSV row after FK checks, or None if skipped."""
        planting_id = row.get("ID", "").strip()
        crop_name = row.get("Crop Name") or row.get("Crop")
        if crop_name:
            crop_name = crop_name.strip()
        block_name = row.get("Block Name") or row.get("Block")
        if block_name:
            block_name = block_name.strip()

        if not crop_name or not block_name:
            return None

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
            return None

        crop_season = self._get_crop_season(crop, block.block_type)
        if not crop_season:
            self.stdout.write(
                self.style.WARNING(
                    f"   ⚠  row {i}: no crop_season for {crop_name}/{block.block_type} — skipping planting"
                )
            )
            self.stats["Planting"]["skipped"] += 1
            return None

        plant_date_str = row.get("Planned Plant Date", "").strip()
        if not plant_date_str:
            self.stats["Planting"]["skipped"] += 1
            return None

        plant_date = self._parse_date(plant_date_str)
        if abs(plant_date.year - year) > 1:
            self._record_planting_date_year_mismatch(year, i, plant_date)
        variety_raw = row.get("Variety", "") or ""
        variety_text = self._normalize_planting_variety_text(variety_raw)
        variety_obj = self._resolve_planting_variety_fk(crop, variety_text)

        bed_start = self._int(row.get("Bed Start", 1))
        bed_end = self._int(row.get("Bed End", 1))
        if bed_end < bed_start:
            bed_start, bed_end = bed_end, bed_start

        status_code = status_map.get(row.get("Status", "planned").strip(), "planned")
        notes = (row.get("Notes") or "").strip()
        planned_bedfeet = self._int(row.get("Planned Bedfeet", 100))

        extras = {}
        actual_plant_date_str = row.get("Actual Plant Date", "").strip()
        if actual_plant_date_str:
            extras["actual_plant_date"] = self._parse_date(actual_plant_date_str)
            extras["actual_bedfeet"] = self._int_or_none(row.get("Actual Bedfeet"))
        actual_harvest_str = row.get("Actual Total Yield", "").strip()
        if actual_harvest_str:
            extras["actual_total_yield"] = self._dec_or_none(actual_harvest_str)

        return {
            "source_line": i,
            "planting_id": planting_id,
            "planning_year": planning_year,
            "crop": crop,
            "block": block,
            "crop_season": crop_season,
            "plant_date": plant_date,
            "variety_text": variety_text,
            "variety_obj": variety_obj,
            "bed_start": bed_start,
            "bed_end": bed_end,
            "planned_bedfeet": planned_bedfeet,
            "status_code": status_code,
            "notes": notes,
            "extras": extras,
        }

    def _dedupe_and_merge_touching_segments(self, items):
        """Within one logical group, merge identical bed intervals and touching/overlapping ranges."""
        by_span = {}
        for it in items:
            key = (it["bed_start"], it["bed_end"])
            if key not in by_span:
                by_span[key] = {
                    "bed_start": it["bed_start"],
                    "bed_end": it["bed_end"],
                    "planned_bedfeet": it["planned_bedfeet"],
                    "source_lines": [it["source_line"]],
                    "notes": [it["notes"]] if it["notes"] else [],
                    "planting_ids": [it["planting_id"]] if it["planting_id"] else [],
                    "extras": [it["extras"]] if it["extras"] else [],
                    "meta": it,
                }
            else:
                agg = by_span[key]
                agg["planned_bedfeet"] += it["planned_bedfeet"]
                agg["source_lines"].append(it["source_line"])
                if it["notes"]:
                    agg["notes"].append(it["notes"])
                if it["planting_id"]:
                    agg["planting_ids"].append(it["planting_id"])
                if it["extras"]:
                    agg["extras"].append(it["extras"])

        segments = sorted(by_span.values(), key=lambda s: (s["bed_start"], s["bed_end"]))
        if not segments:
            return []

        merged = []
        cur = segments[0]
        for seg in segments[1:]:
            if seg["bed_start"] <= cur["bed_end"] + 1:
                cur["bed_end"] = max(cur["bed_end"], seg["bed_end"])
                cur["planned_bedfeet"] += seg["planned_bedfeet"]
                cur["source_lines"].extend(seg["source_lines"])
                cur["notes"].extend(seg["notes"])
                cur["planting_ids"].extend(seg["planting_ids"])
                cur["extras"].extend(seg["extras"])
            else:
                merged.append(cur)
                cur = seg
        merged.append(cur)
        return merged

    def _merge_notes_parts(self, parts):
        seen = set()
        out = []
        for p in parts:
            p = (p or "").strip()
            if not p or p in seen:
                continue
            seen.add(p)
            out.append(p)
        return " | ".join(out)

    def _merge_planting_extras(self, extras_list, representative_line):
        """Pick first non-empty extras; record mismatch if conflicting non-null values differ."""
        merged = {}
        for field in ("actual_plant_date", "actual_bedfeet", "actual_total_yield"):
            values = []
            for ext in extras_list:
                if not ext:
                    continue
                if field in ext and ext[field] is not None:
                    values.append(ext[field])
            uniq = []
            for v in values:
                if v not in uniq:
                    uniq.append(v)
            if len(uniq) > 1:
                self._record_row_error(
                    "Planting",
                    representative_line,
                    code="namespace_mismatch",
                    field_path=f"plantings.{field}",
                    message=f"conflicting {field} values when merging bed range rows",
                )
            if uniq:
                merged[field] = uniq[0]
        return merged

    def _delete_overlapping_plantings(
        self, planning_year, crop, block, plant_date, variety_text, status_code, bed_start, bed_end
    ):
        if self.write_disabled or not isinstance(planning_year, PlanningYear):
            return
        qs = Planting.objects.filter(
            planning_year=planning_year,
            crop=crop,
            block=block,
            planned_plant_date=plant_date,
            status=status_code,
            bed_start__lte=bed_end,
            bed_end__gte=bed_start,
        )
        if variety_text:
            qs = qs.filter(variety__iexact=variety_text)
        else:
            qs = qs.filter(variety="")
        qs.delete()

    def _import_plantings(self, year, year_dir):
        """Import plantings with consecutive-bed consolidation."""
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
            raw_rows = list(reader)

        parsed = []
        for i, row in enumerate(raw_rows, 1):
            try:
                rec = self._parse_single_planting_csv_row(year, i, row, status_map)
                if rec:
                    parsed.append(rec)
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

        buckets = defaultdict(list)
        for rec in parsed:
            key = (
                rec["crop"].id,
                rec["block"].id,
                rec["plant_date"],
                self._planting_variety_merge_key(rec["variety_text"]),
                rec["status_code"],
            )
            buckets[key].append(rec)

        processed_counter = 0
        for _key, items in buckets.items():
            meta0 = items[0]
            segments = self._dedupe_and_merge_touching_segments(items)
            for seg in segments:
                try:
                    rep_line = min(seg["source_lines"])
                    lead_rec = next(r for r in items if r["source_line"] == rep_line)
                    merged_notes = self._merge_notes_parts(seg["notes"])
                    merged_extras = self._merge_planting_extras(seg["extras"], rep_line)
                    planting_ids = [x for x in seg["planting_ids"] if x]
                    if len(set(planting_ids)) > 1:
                        self._record_row_error(
                            "Planting",
                            rep_line,
                            code="namespace_mismatch",
                            field_path="plantings.id",
                            message="multiple distinct ID values for rows merged into one bed range",
                        )

                    data = {
                        "crop_season": meta0["crop_season"],
                        "variety": self._normalize_planting_variety_text(lead_rec["variety_text"]),
                        "variety_obj": lead_rec["variety_obj"],
                        "bed_start": seg["bed_start"],
                        "bed_end": seg["bed_end"],
                        "planned_bedfeet": seg["planned_bedfeet"],
                        "planned_plant_date": meta0["plant_date"],
                        "status": meta0["status_code"],
                        "notes": merged_notes,
                    }
                    data.update(merged_extras)

                    planning_year = meta0["planning_year"]
                    crop = meta0["crop"]
                    block = meta0["block"]
                    plant_date = meta0["plant_date"]

                    if not self.write_disabled:
                        self._delete_overlapping_plantings(
                            planning_year,
                            crop,
                            block,
                            plant_date,
                            data["variety"],
                            data["status"],
                            seg["bed_start"],
                            seg["bed_end"],
                        )
                        obj, created = Planting.objects.update_or_create(
                            planning_year=planning_year,
                            crop=crop,
                            block=block,
                            bed_start=seg["bed_start"],
                            bed_end=seg["bed_end"],
                            planned_plant_date=plant_date,
                            defaults=data,
                        )
                        self.stats["Planting"]["created" if created else "processed"] += 1
                        for pid in set(seg["planting_ids"]):
                            self.planting_cache[pid] = obj
                        if not seg["planting_ids"]:
                            lead_line = min(seg["source_lines"])
                            for rec in items:
                                if rec["source_line"] == lead_line and rec.get("planting_id"):
                                    self.planting_cache[rec["planting_id"]] = obj
                                    break
                    else:
                        self.stats["Planting"]["processed"] += 1

                    processed_counter += 1
                    if processed_counter % self.PLANTING_PROGRESS_EVERY == 0:
                        created_count = self.stats["Planting"].get("created", 0)
                        updated_count = self.stats["Planting"].get("processed", 0)
                        self.stdout.write(
                            f"  ... plantings {year}: merged job {processed_counter}, "
                            f"created={created_count}, updated={updated_count}, "
                            f"skipped={self.stats['Planting']['skipped']}, "
                            f"errors={self.stats['Planting']['errors']}"
                        )

                except (
                    ValueError,
                    KeyError,
                    InvalidOperation,
                    ValidationError,
                    IntegrityError,
                    DatabaseError,
                ) as e:
                    self.stderr.write(f"    ERROR merged planting: {e}")
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

    def _resolve_sales_plan_302_category(self, raw_label: str):
        """Map workbook 302 ``Channel`` column (Markets / Orders / CSA) to SalesCategory name."""
        normalized = self._normalize_lookup_value(raw_label)
        if normalized in {"markets", "market"}:
            return SalesCategory.CategoryName.MARKETS
        if normalized in {"orders", "order", "wholesale"}:
            return SalesCategory.CategoryName.ORDERS
        if normalized == "csa":
            return SalesCategory.CategoryName.CSA
        return None

    def _import_sales_plan_302(self, year, year_dir):
        """Import workbook 302 long-table rows: category + product + harvest ISO week + qty/value.

        Headers: Channel|Product|Harvest Year|Harvest Week|Qty|Value
        ``Channel`` is **sales category** (not an outlet). Rows are stored as ``SalesEvent`` PLAN
        rows with ``sales_category`` set and ``channel`` null.
        """
        path = os.path.join(year_dir, "sales_plan_302.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing Sales Plan 302 (category demand) {year}...")

        planning_year = self._get_planning_year(year)
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                try:
                    cat_raw = (
                        row.get("Channel")
                        or row.get("channel")
                        or row.get("Category")
                        or row.get("category")
                        or ""
                    ).strip()
                    product_name = (row.get("Product") or row.get("Product Name") or "").strip()
                    hy_raw = (
                        row.get("Harvest Year")
                        or row.get("Year")
                        or row.get("harvest_year")
                        or ""
                    ).strip()
                    hw_raw = (
                        row.get("Harvest Week")
                        or row.get("Week")
                        or row.get("harvest_week")
                        or ""
                    ).strip()
                    qty_raw = (row.get("Qty") or row.get("Quantity") or row.get("Planned Quantity") or "").strip()
                    value_raw = (row.get("Value") or row.get("Planned Revenue") or "").strip()

                    if not cat_raw:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "sales_plan_302.category",
                            "Channel",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not product_name:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "sales_plan_302.product",
                            "Product",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not hy_raw or not hw_raw:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "sales_plan_302.harvest_week",
                            "Harvest Year / Harvest Week",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue
                    if not qty_raw:
                        self._record_missing_required(
                            "SalesEvent",
                            i,
                            "sales_plan_302.qty",
                            "Qty",
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    cat_name = self._resolve_sales_plan_302_category(cat_raw)
                    if cat_name is None:
                        message = f"unknown sales category in Channel column '{cat_raw}'"
                        self.stderr.write(f"    ERROR row {i}: {message}")
                        self._record_row_error(
                            "SalesEvent",
                            i,
                            code="namespace_mismatch",
                            field_path="sales_plan_302.channel",
                            message=message,
                        )
                        self.stats["SalesEvent"]["errors"] += 1
                        continue

                    harvest_year = self._int(hy_raw, 0)
                    week = self._int(hw_raw, 0)
                    if harvest_year != year:
                        message = f"Harvest Year {harvest_year} does not match import year folder {year}"
                        self.stderr.write(f"    ERROR row {i}: {message}")
                        self._record_row_error(
                            "SalesEvent",
                            i,
                            code="namespace_mismatch",
                            field_path="sales_plan_302.harvest_year",
                            message=message,
                        )
                        self.stats["SalesEvent"]["errors"] += 1
                        continue
                    if week < 1 or week > 53:
                        message = f"invalid harvest week '{hw_raw}'"
                        self.stderr.write(f"    ERROR row {i}: {message}")
                        self._record_row_error(
                            "SalesEvent",
                            i,
                            code="namespace_mismatch",
                            field_path="sales_plan_302.harvest_week",
                            message=message,
                        )
                        self.stats["SalesEvent"]["errors"] += 1
                        continue

                    product = self._get_product_by_name(product_name)
                    if not product:
                        self._record_stale_fk(
                            "SalesEvent",
                            i,
                            "sales_plan_302.product",
                            "product",
                            product_name,
                        )
                        self.stats["SalesEvent"]["skipped"] += 1
                        continue

                    category = self._ensure_sales_category(cat_name)
                    sale_date = datetime.fromisocalendar(harvest_year, week, 1).date()
                    planned_quantity = self._dec(qty_raw)
                    if value_raw:
                        planned_revenue = self._dec(value_raw)
                    else:
                        planned_revenue = planned_quantity * product.sale_price

                    defaults = {
                        "planning_year": planning_year,
                        "entry_kind": SalesEvent.EntryKind.PLAN,
                        "channel": None,
                        "planned_quantity": planned_quantity,
                        "planned_revenue": planned_revenue,
                        "notes": (row.get("Notes") or "").strip(),
                    }

                    if not self.write_disabled:
                        _, created = SalesEvent.objects.update_or_create(
                            entry_kind=SalesEvent.EntryKind.PLAN,
                            planning_year=planning_year,
                            channel=None,
                            sales_category=category,
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
        """Nursery events are derived from ``CropInfo`` + ``Planting`` (not imported from CSV)."""
        path = os.path.join(year_dir, "nursery_events.csv")
        if os.path.exists(path):
            self.stdout.write(
                self.style.WARNING(
                    f"  ⊘ year_{year}/nursery_events.csv ignored — nursery is derived-only "
                    f"(use ``validate_nursery_sheet_parity`` to compare sheet vs derived)\n"
                )
            )
        # Prime summary counters so JSON artifacts always include a ``NurseryEvent`` bucket.
        if getattr(self, "stats", None) is not None:
            _ = self.stats["NurseryEvent"]

    def _nursery_row_is_workbook_402_plan_tab_shape(self, row):
        return nursery_plan_sheet.nursery_row_is_workbook_402_plan_tab_shape(row)

    def _nursery_sheet_crop_variety_cell(self, row):
        return nursery_plan_sheet.nursery_sheet_crop_variety_cell(row)

    def _split_crop_variety_for_nursery_sheet(self, cell):
        return nursery_plan_sheet.split_crop_variety_for_nursery_sheet(cell)

    def _date_from_plan_year_week(self, year_raw, week_raw):
        return nursery_plan_sheet.date_from_plan_year_week(year_raw, week_raw)

    def _int_from_tray_size_cell(self, raw):
        return nursery_plan_sheet.int_from_tray_size_cell(raw)

    def _resolve_nursery_planting_from_plan_tab(self, row, year):
        return nursery_plan_sheet.resolve_nursery_planting_from_plan_tab(row, year)

    def _nursery_upsert_event(self, planting, event_type, planned_date, save_defaults):
        """Persist one nursery event (``save_defaults`` excludes lookup key fields)."""
        if not self.write_disabled:
            _, created = NurseryEvent.objects.update_or_create(
                planting=planting,
                planned_date=planned_date,
                event_type=event_type,
                defaults=save_defaults,
            )
            self.stats["NurseryEvent"]["created" if created else "processed"] += 1
        else:
            self.stats["NurseryEvent"]["processed"] += 1

    def _import_nursery_events_workbook_402_plan_tab_row(self, i, row, year):
        """Import seed / pot-up events from one ``Nursery Plan 502`` wide row."""
        planting_id = (row.get("Planting ID") or "").strip()
        planting = self._get_planting(planting_id) if planting_id else None
        if not planting:
            planting = self._resolve_nursery_planting_from_plan_tab(row, year)
        if not planting:
            self.stats["NurseryEvent"]["skipped"] += 1
            return

        note_lines = []
        for label, col in (
            ("Germ temp", "Germ Temp"),
            ("Days to germ", "Days To Germ"),
            ("Germ notes", "Germ Notes"),
            ("Nursery seeding notes", "Nursery Seeding Notes"),
        ):
            v = (row.get(col) or "").strip()
            if v:
                note_lines.append(f"{label}: {v}")
        spc = (row.get("Seeds Per Cell") or "").strip()
        if spc:
            note_lines.append(f"Seeds per cell: {spc}")
        seed_notes = "\n".join(note_lines)

        seed_y = (row.get("Nursery Seeding Year") or "").strip()
        seed_w = (row.get("Nursery Seeding Week") or "").strip()
        if seed_y and seed_w:
            seed_date = self._date_from_plan_year_week(seed_y, seed_w)
            if seed_date:
                save_defaults = {
                    "planned_tray_count": self._int_or_none(row.get("Trays To Seed")),
                    "planned_tray_size": self._int_from_tray_size_cell(row.get("Seeded Tray Size")),
                    "notes": seed_notes,
                }
                self._nursery_upsert_event(planting, "seed", seed_date, save_defaults)

        pot_y = (row.get("Nursery Pot Up Year") or "").strip()
        pot_w = (row.get("Nursery Pot Up Week") or "").strip()
        if pot_y and pot_w:
            pot_date = self._date_from_plan_year_week(pot_y, pot_w)
            if pot_date:
                save_defaults = {
                    "planned_tray_count": self._int_or_none(row.get("Trays To Pot Up")),
                    "planned_tray_size": self._int_from_tray_size_cell(row.get("Pot Up Tray Size")),
                    "notes": "",
                }
                self._nursery_upsert_event(planting, "pot_up", pot_date, save_defaults)

    def _import_nursery_events_canonical_row(self, i, row):
        """Import one canonical ``nursery_events.csv`` row (``Planting ID`` + ``Planned Date``)."""
        planting_id = row.get("Planting ID", "").strip()
        planting = self._get_planting(planting_id)

        if not planting:
            self.stats["NurseryEvent"]["skipped"] += 1
            return

        event_type = row.get("Event Type", "").strip().lower()
        if event_type not in ("seed", "pot_up", "harden", "transplant"):
            event_type = "seed"

        planned_date_str = row.get("Planned Date", "").strip()
        if not planned_date_str:
            self.stats["NurseryEvent"]["skipped"] += 1
            return

        planned_date = self._parse_date(planned_date_str)
        save_defaults = {
            "planned_tray_count": self._int_or_none(row.get("Planned Tray Count")),
            "planned_tray_size": self._int_or_none(row.get("Planned Tray Size")),
        }

        actual_date_str = row.get("Actual Date", "").strip()
        if actual_date_str:
            save_defaults["actual_date"] = self._parse_date(actual_date_str)
            save_defaults["actual_tray_count"] = self._int_or_none(row.get("Actual Tray Count"))
            save_defaults["actual_tray_size"] = self._int_or_none(row.get("Actual Tray Size"))
            save_defaults["actual_germination_rate"] = self._dec_or_none(
                row.get("Actual Germination Rate")
            )

        save_defaults["notes"] = row.get("Notes", "").strip()

        self._nursery_upsert_event(planting, event_type, planned_date, save_defaults)

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
            self._import_pack_batch_components(year, year_dir)
            self._import_pack_allocations(year, year_dir)

    def _import_field_walk_notes(self, year, year_dir):
        """Import field walk notes."""
        path = os.path.join(year_dir, "field_walk_notes.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing field walk notes {year}...")

        # Match CSV "Condition" case-insensitively (see FieldWalkNote.CONDITION_CHOICES)
        condition_map = {
            "ahead": "ahead",
            "ahead of plan": "ahead",
            "behind": "behind",
            "behind plan": "behind",
            "good": "good",
            "fair": "fair",
            "poor": "poor",
            "failed": "failed",
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

                    condition_raw = (row.get("Condition") or "good").strip()
                    condition = condition_map.get(condition_raw.lower(), "good")

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
                    finished_true = fin in ("TRUE", "1", "YES", "Y")
                    if finished_true:
                        updates["status"] = PlantingStatus.COMPLETE
                    elif date_str and planting.status == PlantingStatus.PLANNED:
                        # 501 Field Records: first in-field actual marks planned rows planted;
                        # subsequent field-walk notes advance planted -> growing in the UI.
                        updates["status"] = PlantingStatus.PLANTED

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
        """Resolve FieldWalkNote planting when Planting ID is missing."""
        return nursery_plan_sheet.resolve_field_walk_planting_from_context(row, year)

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

    def _import_pack_batch_components(self, year, year_dir):
        """Import executed mix lines from ``year_YYYY/pack_batch_components.csv`` (601 H1b)."""
        path = os.path.join(year_dir, "pack_batch_components.csv")
        if not os.path.exists(path):
            return

        self.stdout.write(f"Importing pack batch components {year}...")

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            rows = list(enumerate(reader, 1))

        groups = defaultdict(list)
        for line_no, row in rows:
            mix_name = (row.get("Mix Product Name") or row.get("Mix Product") or "").strip()
            pack_date_raw = (row.get("Pack Date") or "").strip()
            if not mix_name or not pack_date_raw:
                missing_fields = []
                if not mix_name:
                    missing_fields.append("Mix Product Name")
                if not pack_date_raw:
                    missing_fields.append("Pack Date")
                self._record_row_warning(
                    "PackBatch",
                    line_no,
                    "skipped_missing_required",
                    "pack_batch_components",
                    f"skipped pack batch row: missing {', '.join(missing_fields)}",
                )
                self._record_skip_reason(
                    "PackBatch",
                    line_no,
                    "skipped_missing_required",
                    "pack_batch_components",
                    f"skipped pack batch row: missing {', '.join(missing_fields)}",
                )
                self.stats["PackBatch"]["skipped"] += 1
                continue
            try:
                pack_d = self._parse_date_loose(pack_date_raw)
            except ValueError:
                self._record_row_error(
                    "PackBatch",
                    line_no,
                    code="missing_required",
                    field_path="pack_batch_components.pack_date",
                    message=f"unparseable Pack Date {pack_date_raw!r}",
                )
                self._record_skip_reason(
                    "PackBatch",
                    line_no,
                    "missing_required",
                    "pack_batch_components.pack_date",
                    f"unparseable Pack Date {pack_date_raw!r}",
                )
                self.stats["PackBatch"]["skipped"] += 1
                continue
            py_raw = (row.get("Planning Year") or "").strip()
            plan_y_int = self._int(py_raw) if py_raw else pack_d.year
            iso_y, iso_w, _ = pack_d.isocalendar()
            groups[(plan_y_int, mix_name, iso_y, iso_w)].append((line_no, row, pack_d))

        for (plan_y_int, mix_name, iso_y, iso_w), group_rows in groups.items():
            try:
                week_monday = date.fromisocalendar(iso_y, iso_w, 1)
            except ValueError:
                for line_no, _, _ in group_rows:
                    self._record_row_error(
                        "PackBatch",
                        line_no,
                        code="namespace_mismatch",
                        field_path="pack_batch_components.pack_date",
                        message="invalid ISO week for pack batch grouping",
                    )
                    self._record_skip_reason(
                        "PackBatch",
                        line_no,
                        "namespace_mismatch",
                        "pack_batch_components.pack_date",
                        "invalid ISO week for pack batch grouping",
                    )
                self.stats["PackBatch"]["skipped"] += len(group_rows)
                continue

            product = self._get_product_by_name(mix_name)
            if not product:
                matches = self._csf_matches_for_mix_label(None, mix_name)
                if len(matches) == 1:
                    product = matches[0]
                elif matches and len({m.crop_id for m in matches}) == 1:
                    product = matches[0]
            if not product:
                for line_no, _, _ in group_rows:
                    self._record_stale_fk(
                        "PackBatch",
                        line_no,
                        "pack_batch_components.mix_product",
                        "product",
                        mix_name,
                    )
                    self._record_skip_reason(
                        "PackBatch",
                        line_no,
                        "stale_fk",
                        "pack_batch_components.mix_product",
                        f"product not found '{mix_name}'",
                    )
                self.stats["PackBatch"]["skipped"] += len(group_rows)
                continue

            ppq_vals = []
            for _ln, r, _ in group_rows:
                cell = (r.get("Planned Pack Quantity") or "").strip()
                if cell:
                    try:
                        ppq_vals.append(self._dec(cell))
                    except (InvalidOperation, ValueError):
                        pass
            packed_qty = None
            if ppq_vals and len({str(x) for x in ppq_vals}) == 1:
                packed_qty = ppq_vals[0]
            sentinel_packed = packed_qty is None or packed_qty <= 0
            if sentinel_packed:
                packed_qty = Decimal("1")
                self._record_row_error(
                    "PackBatch",
                    group_rows[0][0],
                    code="namespace_mismatch",
                    field_path="pack_batch_components.packed_quantity",
                    message="using sentinel packed_quantity=1 (H1b10.1); reconcile from SalesEvent if needed",
                )
            packed_unit = (product.sale_unit or "unit").strip()[:20] or "unit"
            pu_cell = (group_rows[0][1].get("Planned Pack Unit") or "").strip()
            if pu_cell:
                packed_unit = pu_cell[:20]

            if self.write_disabled:
                self.stats["PackBatch"]["processed"] += 1
                self.stats["PackBatchComponent"]["processed"] += len(group_rows)
                continue

            py_obj = self._ensure_planning_year(plan_y_int)
            batch, created = PackBatch.objects.update_or_create(
                planning_year=py_obj,
                product=product,
                pack_date=week_monday,
                recipe=None,
                defaults={
                    "packed_quantity": packed_qty,
                    "packed_unit": packed_unit,
                    "notes": "Imported from pack_batch_components.csv",
                },
            )
            self.stats["PackBatch"]["created" if created else "processed"] += 1

            batch.components.all().delete()
            for line_no, row, _pack_d in group_rows:
                st_raw = (
                    row.get("Component Source Type") or row.get("Source Type") or "crop"
                ).strip().casefold()
                if st_raw in ("crop", "c"):
                    source_kind = "crop"
                elif st_raw in ("product", "p"):
                    source_kind = "product"
                else:
                    self._record_row_error(
                        "PackBatchComponent",
                        line_no,
                        code="namespace_mismatch",
                        field_path="pack_batch_components.component_source_type",
                        message=f"expected crop or product, got {st_raw!r}",
                    )
                    self.stats["PackBatchComponent"]["errors"] += 1
                    continue

                comp_crop_name = (
                    row.get("Component Crop Name") or row.get("Component Crop") or ""
                ).strip()
                comp_product_name = (
                    row.get("Component Product Name") or row.get("Component Product") or ""
                ).strip()
                pct_raw = (row.get("Component Percent") or row.get("Percent") or "").strip()
                qty_raw = (row.get("Component Quantity") or row.get("Quantity") or "").strip()
                unit_raw = (row.get("Component Unit") or row.get("Unit") or "").strip()

                pct = None
                if pct_raw:
                    try:
                        pct = self._dec(pct_raw)
                        if pct <= 0:
                            pct = None
                        elif pct > Decimal("100"):
                            raise ValueError("percent range")
                    except (InvalidOperation, ValueError):
                        self._record_row_error(
                            "PackBatchComponent",
                            line_no,
                            code="namespace_mismatch",
                            field_path="pack_batch_components.component_percent",
                            message=f"invalid percent {pct_raw!r}",
                        )
                        self.stats["PackBatchComponent"]["errors"] += 1
                        continue

                qty = None
                if qty_raw:
                    try:
                        qty = self._dec(qty_raw)
                        if qty <= 0:
                            self._record_row_warning(
                                "PackBatchComponent",
                                line_no,
                                "skipped_non_positive_quantity",
                                "pack_batch_components.component_quantity",
                                f"skipped component row: non-positive quantity {qty_raw!r}",
                            )
                            self._record_skip_reason(
                                "PackBatchComponent",
                                line_no,
                                "skipped_non_positive_quantity",
                                "pack_batch_components.component_quantity",
                                f"skipped component row: non-positive quantity {qty_raw!r}",
                            )
                            self.stats["PackBatchComponent"]["skipped"] += 1
                            continue
                        qty = qty.quantize(Decimal("0.01"))
                    except (InvalidOperation, ValueError):
                        self._record_row_error(
                            "PackBatchComponent",
                            line_no,
                            code="namespace_mismatch",
                            field_path="pack_batch_components.component_quantity",
                            message=f"invalid quantity {qty_raw!r}",
                        )
                        self.stats["PackBatchComponent"]["errors"] += 1
                        continue

                if pct is None and qty is None:
                    self._record_skip_reason(
                        "PackBatchComponent",
                        line_no,
                        "skipped_missing_component_amount",
                        "pack_batch_components.component_percent",
                        "skipped component row: requires positive Component Percent or Component Quantity",
                    )
                    self.stats["PackBatchComponent"]["skipped"] += 1
                    continue

                if qty is not None and not unit_raw:
                    unit_raw = product.sale_unit or "unit"
                if pct is not None and qty is None:
                    qty = Decimal("1")
                    unit_raw = unit_raw or product.sale_unit or "unit"

                src_crop = None
                src_product = None
                if source_kind == "crop":
                    if comp_product_name:
                        self._record_row_error(
                            "PackBatchComponent",
                            line_no,
                            code="namespace_mismatch",
                            field_path="pack_batch_components.component_product",
                            message="Component Product must be empty when source type is crop",
                        )
                        self.stats["PackBatchComponent"]["errors"] += 1
                        continue
                    if not comp_crop_name:
                        self._record_missing_required(
                            "PackBatchComponent",
                            line_no,
                            "pack_batch_components.component_crop",
                            "Component Crop Name",
                        )
                        self._record_skip_reason(
                            "PackBatchComponent",
                            line_no,
                            "missing_required",
                            "pack_batch_components.component_crop",
                            "missing required value for 'Component Crop Name'",
                        )
                        self.stats["PackBatchComponent"]["skipped"] += 1
                        continue
                    crop_obj = self._get_crop(comp_crop_name)
                    if not crop_obj:
                        self._record_stale_fk(
                            "PackBatchComponent",
                            line_no,
                            "pack_batch_components.component_crop",
                            "crop",
                            comp_crop_name,
                        )
                        self._record_skip_reason(
                            "PackBatchComponent",
                            line_no,
                            "stale_fk",
                            "pack_batch_components.component_crop",
                            f"crop not found '{comp_crop_name}'",
                        )
                        self.stats["PackBatchComponent"]["skipped"] += 1
                        continue
                    src_crop = crop_obj
                else:
                    if not comp_crop_name or not comp_product_name:
                        self._record_missing_required(
                            "PackBatchComponent",
                            line_no,
                            "pack_batch_components.component_product",
                            "Component Crop Name and Component Product Name",
                        )
                        self._record_skip_reason(
                            "PackBatchComponent",
                            line_no,
                            "missing_required",
                            "pack_batch_components.component_product",
                            "missing required value for 'Component Crop Name and Component Product Name'",
                        )
                        self.stats["PackBatchComponent"]["skipped"] += 1
                        continue
                    c_crop = self._get_crop(comp_crop_name)
                    if not c_crop:
                        self._record_stale_fk(
                            "PackBatchComponent",
                            line_no,
                            "pack_batch_components.component_crop",
                            "crop",
                            comp_crop_name,
                        )
                        self._record_skip_reason(
                            "PackBatchComponent",
                            line_no,
                            "stale_fk",
                            "pack_batch_components.component_crop",
                            f"crop not found '{comp_crop_name}'",
                        )
                        self.stats["PackBatchComponent"]["skipped"] += 1
                        continue
                    csf_comp = CropSalesFormat.objects.filter(
                        crop=c_crop, product_name=comp_product_name
                    ).first()
                    if not csf_comp:
                        self._record_stale_fk(
                            "PackBatchComponent",
                            line_no,
                            "pack_batch_components.component_product",
                            "product",
                            f"{comp_crop_name} / {comp_product_name}",
                        )
                        self._record_skip_reason(
                            "PackBatchComponent",
                            line_no,
                            "stale_fk",
                            "pack_batch_components.component_product",
                            f"product not found '{comp_crop_name} / {comp_product_name}'",
                        )
                        self.stats["PackBatchComponent"]["skipped"] += 1
                        continue
                    src_product = csf_comp

                note_parts = []
                vr = (row.get("Variety Request") or "").strip()
                if vr:
                    note_parts.append(f"Variety: {vr}")
                sf = (row.get("Safety Factor") or "").strip()
                if sf and sf not in ("1", "1.0", "1.00"):
                    note_parts.append(f"Safety factor: {sf}")
                hd = self._parse_date_optional(row.get("Harvest Date"))
                if hd and hd != week_monday:
                    note_parts.append(f"Harvest date: {hd.isoformat()}")
                base_notes = (row.get("Notes") or "").strip()
                if base_notes:
                    note_parts.append(base_notes)
                comp_notes = "\n".join(note_parts)

                comp = PackBatchComponent(
                    pack_batch=batch,
                    source_crop=src_crop,
                    source_product=src_product,
                    consumed_quantity=qty,
                    consumed_unit=(unit_raw or product.sale_unit or "unit")[:20],
                    component_percent=pct,
                    notes=comp_notes,
                )
                try:
                    comp.full_clean()
                    comp.save()
                    self.stats["PackBatchComponent"]["created"] += 1
                except (ValidationError, IntegrityError, DatabaseError) as e:
                    self._record_row_error(
                        "PackBatchComponent",
                        line_no,
                        code="namespace_mismatch",
                        field_path="pack_batch_components.row",
                        message=str(e),
                    )
                    self.stats["PackBatchComponent"]["errors"] += 1

        self.stdout.write(
            f" {self.stats['PackBatch'].get('processed', 0) + self.stats['PackBatch'].get('created', 0)} pack batches, "
            f"{self.stats['PackBatchComponent'].get('created', 0)} components, "
            f"{self.stats['PackBatch'].get('skipped', 0)} skipped batches, "
            f"{self.stats['PackBatchComponent'].get('skipped', 0)} skipped components, "
            f"{self.stats['PackBatchComponent'].get('errors', 0)} component errors\n"
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
                            self._record_skip_reason(
                                "PackAllocation",
                                i,
                                "missing_required",
                                "pack_allocations.channel",
                                "missing required value for 'Channel'",
                            )
                        if not product_name:
                            self._record_missing_required(
                                "PackAllocation",
                                i,
                                "pack_allocations.product",
                                "Product",
                            )
                            self._record_skip_reason(
                                "PackAllocation",
                                i,
                                "missing_required",
                                "pack_allocations.product",
                                "missing required value for 'Product'",
                            )
                        if not pack_date_str:
                            self._record_missing_required(
                                "PackAllocation",
                                i,
                                "pack_allocations.pack_date",
                                "Pack Date",
                            )
                            self._record_skip_reason(
                                "PackAllocation",
                                i,
                                "missing_required",
                                "pack_allocations.pack_date",
                                "missing required value for 'Pack Date'",
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
                        self._record_skip_reason(
                            "PackAllocation",
                            i,
                            "stale_fk",
                            "pack_allocations.channel",
                            f"sales channel not found '{channel_name}'",
                        )
                        self.stats["PackAllocation"]["skipped"] += 1
                        continue
                    if not self._has_channel_rollup_assignment(
                        "PackAllocation",
                        i,
                        "pack_allocations.channel_rollup",
                        channel.name,
                    ):
                        self._record_skip_reason(
                            "PackAllocation",
                            i,
                            "channel_rollup_mismatch",
                            "pack_allocations.channel_rollup",
                            f"channel '{channel.name}' is missing a sales category rollup assignment",
                        )
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
                        self._record_skip_reason(
                            "PackAllocation",
                            i,
                            "stale_fk",
                            "pack_allocations.product",
                            f"product not found '{product_name}'",
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
                        recipe = self._get_recipe_for_product(
                            product, recipe_name, pack_date_year=data["pack_date"].year
                        )
                        if not recipe:
                            self._record_stale_fk(
                                "PackAllocation",
                                i,
                                "pack_allocations.recipe",
                                "mix recipe",
                                recipe_name,
                            )
                            self._record_skip_reason(
                                "PackAllocation",
                                i,
                                "stale_fk",
                                "pack_allocations.recipe",
                                f"mix recipe not found '{recipe_name}'",
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
                            self._record_skip_reason(
                                "PackAllocation",
                                i,
                                "missing_required",
                                "pack_allocations.packed_quantity",
                                "missing required value for 'Packed Quantity'",
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
                            pack_py = recipe.planning_year or self._ensure_planning_year(
                                data["pack_date"].year
                            )
                            pack_batch, _ = PackBatch.objects.update_or_create(
                                product=product,
                                recipe=recipe,
                                pack_date=data["pack_date"],
                                defaults={
                                    "planning_year": pack_py,
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
        self._warn_year_folders_outside_import_range()
        self._reconcile_duplicate_sales_channels()
        for year in range(self.start_year, self.end_year + 1):
            year_dir = os.path.join(self.data_dir, f"year_{year}")
            if not os.path.isdir(year_dir):
                continue

            self._import_sales_events(year, year_dir)
            self._import_quick_sales_entries(year, year_dir)

        self._import_rotation_history()

    def _warn_year_folders_outside_import_range(self) -> None:
        """LIVE-10: on-disk ``year_YYYY/`` is skipped entirely when YYYY is outside start/end (e.g. 2023 601)."""
        try:
            names = os.listdir(self.data_dir)
        except OSError:
            return
        skipped: list[int] = []
        for name in names:
            m = re.match(r"^year_(\d{4})$", name)
            if not m or not os.path.isdir(os.path.join(self.data_dir, name)):
                continue
            y = int(m.group(1))
            if y < self.start_year or y > self.end_year:
                skipped.append(y)
        for y in sorted(skipped):
            message = (
                f"Directory year_{y}/ exists under the bundle root but is outside "
                f"--start-year {self.start_year} --end-year {self.end_year}; "
                "this tier (including year_<Y>/sales_events.csv) is not processed. "
                "Widen the import year range to pick up 601 / historical sales for that season."
            )
            self.stdout.write(self.style.WARNING(f"  ⚠  {message}"))
            self.row_warnings.append(
                {
                    "model": "import_historical_data",
                    "row": 1,
                    "code": "import_year_range_skips_disk_year_folder",
                    "field_path": f"data_dir/year_{y}",
                    "message": message,
                }
            )

    def _reconcile_duplicate_sales_channels(self):
        """Repoint sales/ops channel FKs to one canonical row per normalized channel name.

        LIVE-1: historical ACTUAL rows can be split across duplicate SalesChannel ids with the
        same operator-facing name. Weekly order strict channel joins then miss prior-year rows.
        During apply runs, collapse FK usage to the oldest id for each normalized name.
        """
        if self.write_disabled:
            return

        rows = list(SalesChannel.objects.all().only("id", "name").order_by("id"))
        if len(rows) < 2:
            return

        by_name = defaultdict(list)
        for row in rows:
            key = self._normalize_lookup_value(row.name)
            if key:
                by_name[key].append(row)

        merged_groups = 0
        for group in by_name.values():
            if len(group) < 2:
                continue
            canonical = group[0]
            moved_sales = 0
            moved_quick = 0
            moved_allocations = 0
            for duplicate in group[1:]:
                moved_sales += self._merge_sales_events_to_canonical_channel(canonical, duplicate)
                moved_quick += self._merge_quick_sales_to_canonical_channel(canonical, duplicate)
                moved_allocations += PackAllocation.objects.filter(channel=duplicate).update(
                    channel=canonical
                )
            merged_groups += 1
            self.stdout.write(
                self.style.WARNING(
                    "   ⚙  Reconciled duplicate sales channels "
                    f"'{canonical.name}' -> canonical id={canonical.id}; "
                    f"moved sales_events={moved_sales}, quick_sales_entries={moved_quick}, "
                    f"pack_allocations={moved_allocations}"
                )
            )
            # Keep cache deterministic for all previously seen aliases.
            for alias in group:
                self.channel_cache[alias.name] = canonical

        if merged_groups:
            self.normalized_lookup_indexes.pop("reference.saleschannel:name", None)

    def _merge_sales_events_to_canonical_channel(self, canonical, duplicate):
        moved = 0
        for event in SalesEvent.objects.filter(channel=duplicate).order_by("id"):
            existing = SalesEvent.objects.filter(
                entry_kind=event.entry_kind,
                channel=canonical,
                sale_date=event.sale_date,
                product=event.product,
                sales_category=event.sales_category,
            ).exclude(id=event.id).first()
            if existing:
                self._merge_sales_event_values(existing, event)
                event.delete()
                moved += 1
                continue
            event.channel = canonical
            event.save(skip_inventory_ledger_sync=True)
            moved += 1
        return moved

    def _merge_sales_event_values(self, target, source):
        fields = (
            "planning_year",
            "harvest_date",
            "planned_quantity",
            "planned_revenue",
            "actual_quantity",
            "actual_revenue",
            "actual_price",
            "brought_quantity",
            "returned_quantity",
            "notes",
            "pack_batch",
            "drawn_from_return",
        )
        changed_fields = []
        for field in fields:
            target_value = getattr(target, field)
            source_value = getattr(source, field)
            if target_value in (None, "") and source_value not in (None, ""):
                setattr(target, field, source_value)
                changed_fields.append(field)
        if changed_fields:
            target.save(skip_inventory_ledger_sync=True, update_fields=changed_fields)

    def _merge_quick_sales_to_canonical_channel(self, canonical, duplicate):
        moved = 0
        for quick in QuickSalesEntry.objects.filter(channel=duplicate).order_by("id"):
            existing = QuickSalesEntry.objects.filter(
                channel=canonical,
                sale_date=quick.sale_date,
            ).exclude(id=quick.id).first()
            if existing:
                existing.total_cash = (existing.total_cash or Decimal("0")) + (
                    quick.total_cash or Decimal("0")
                )
                existing.total_card = (existing.total_card or Decimal("0")) + (
                    quick.total_card or Decimal("0")
                )
                if quick.notes:
                    existing.notes = "\n".join(
                        [piece for piece in [existing.notes, quick.notes] if piece]
                    )
                existing.save(update_fields=["total_cash", "total_card", "notes"])
                quick.delete()
                moved += 1
                continue
            quick.channel = canonical
            quick.save(update_fields=["channel"])
            moved += 1
        return moved

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
                        if self._sales_event_row_is_formula_skeleton(row):
                            self.stats["SalesEvent"]["skipped"] += 1
                            continue
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

                    try:
                        sale_date_parsed = self._parse_date_loose(sale_date_str)
                    except ValueError:
                        sale_date_parsed = self._parse_date(sale_date_str)

                    data = {
                        "channel": channel,
                        "sale_date": sale_date_parsed,
                    }

                    plan_y_raw = (
                        row.get("Planning Year") or row.get("Distribution Year") or ""
                    ).strip()
                    plan_year_int = self._int(plan_y_raw) if plan_y_raw else sale_date_parsed.year
                    data["planning_year"] = self._ensure_planning_year(plan_year_int)

                    hd = self._parse_date_optional(row.get("Harvest Date"))
                    if hd:
                        data["harvest_date"] = hd

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

                    # Planned fields (allow zero quantities)
                    planned_qty = row.get("Planned Quantity", "").strip()
                    if planned_qty != "":
                        pq = self._csv_decimal_allow_zero(planned_qty)
                        if pq is not None:
                            data["planned_quantity"] = pq

                    planned_rev = row.get("Planned Revenue", "").strip()
                    if planned_rev != "":
                        pr = self._csv_decimal_allow_zero(planned_rev)
                        if pr is not None:
                            data["planned_revenue"] = pr

                    # Actual fields
                    actual_qty = row.get("Actual Quantity", "").strip()
                    if actual_qty != "":
                        aq = self._csv_decimal_allow_zero(actual_qty)
                        if aq is not None:
                            data["actual_quantity"] = aq

                    actual_rev = row.get("Actual Revenue", "").strip()
                    if actual_rev != "":
                        ar = self._csv_decimal_allow_zero(actual_rev)
                        if ar is not None:
                            data["actual_revenue"] = ar

                    actual_price = row.get("Actual Price", "").strip()
                    if actual_price != "":
                        ap = self._csv_decimal_allow_zero(actual_price)
                        if ap is not None:
                            data["actual_price"] = ap

                    # Brought/returned
                    bq = row.get("Brought Quantity")
                    if bq is not None and str(bq).strip() != "":
                        data["brought_quantity"] = self._csv_decimal_allow_zero(bq)
                    rq = row.get("Returned Quantity")
                    if rq is not None and str(rq).strip() != "":
                        data["returned_quantity"] = self._csv_decimal_allow_zero(rq)

                    ek_raw = (row.get("Entry Kind") or row.get("entry_kind") or "actual").strip().casefold()
                    if ek_raw == "plan":
                        data["entry_kind"] = SalesEvent.EntryKind.PLAN
                    else:
                        data["entry_kind"] = SalesEvent.EntryKind.ACTUAL

                    notes_val = (row.get("Notes") or "").strip()
                    variety_val = (row.get("Variety Request") or "").strip()
                    if variety_val:
                        if notes_val:
                            notes_val = f"{notes_val}\nVariety: {variety_val}"
                        else:
                            notes_val = f"Variety: {variety_val}"
                    data["notes"] = notes_val

                    product_obj = data.get("product")
                    if (
                        product_obj
                        and data.get("entry_kind") == SalesEvent.EntryKind.ACTUAL
                        and not self.write_disabled
                    ):
                        batch = self._get_pack_batch(channel.id, product_obj.id, data["sale_date"])
                        if batch:
                            data["pack_batch"] = batch

                    if not self.write_disabled:
                        obj = SalesEvent.objects.filter(
                            entry_kind=data["entry_kind"],
                            channel=channel,
                            sale_date=data["sale_date"],
                            product=product_obj,
                        ).first()
                        created = obj is None
                        if created:
                            obj = SalesEvent(
                                entry_kind=data["entry_kind"],
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

    def _sales_event_row_is_formula_skeleton(self, row):
        """True for empty 601 formula rows that only carry default zero expressions."""
        identity_fields = [
            "Channel Name",
            "Channel",
            "Sale Date",
            "Product Name",
            "Product",
            "Planning Year",
            "Distribution Year",
            "Harvest Date",
            "Notes",
            "Variety Request",
        ]
        if any((row.get(name) or "").strip() for name in identity_fields):
            return False
        quantity_fields = [
            "Planned Quantity",
            "Planned Revenue",
            "Actual Quantity",
            "Actual Revenue",
            "Actual Price",
            "Brought Quantity",
            "Returned Quantity",
        ]
        if not any((row.get(name) or "").strip() for name in quantity_fields):
            return False
        entry_kind = (row.get("Entry Kind") or row.get("entry_kind") or "").strip().casefold()
        if entry_kind and entry_kind not in ("actual", "plan"):
            return False
        for name in quantity_fields:
            value = (row.get(name) or "").strip()
            if value and self._csv_decimal_allow_zero(value) not in (None, Decimal("0")):
                return False
        return True

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

    def _upsert_sales_channel_by_name(self, name, defaults):
        """Upsert SalesChannel by name while tolerating pre-existing duplicate names."""
        existing = SalesChannel.objects.filter(name=name).order_by("id").first()
        if existing:
            for field, value in defaults.items():
                setattr(existing, field, value)
            existing.save(update_fields=list(defaults.keys()))
            created = False
            return existing, created
        obj = SalesChannel.objects.create(name=name, **defaults)
        return obj, True

    def _get_product_by_name(self, product_name):
        """Get crop sales format by product name."""
        if product_name in self.product_cache:
            return self.product_cache[product_name]
        lookup_name = product_name
        if product_name:
            alias_key = self._normalize_lookup_value(product_name)
            if alias_key in self.product_name_aliases:
                lookup_name = self.product_name_aliases[alias_key]
        resolved = self._resolve_fk_by_text(
            CropSalesFormat,
            "product_name",
            lookup_name,
            label="product",
        )
        if (
            resolved is None
            and product_name
            and not self.validate_only
            and not self.write_disabled
        ):
            resolved = self._ensure_design_time_crop_sales_format(product_name.strip())
        self.product_cache[product_name] = resolved
        return resolved

    def _ensure_design_time_crop_sales_format(self, stripped: str):
        """Create ``CropSalesFormat`` when Sales Plan names a product not yet in reference CSV.

        Resolves the longest ``CropInfo`` name that prefixes the plan ``product_name`` and checks
        ``compose_crop_sales_format_product_name`` agreement so rows like ``Cucumber Persian - lb``
        work once ``Crop Info`` lists ``Cucumber Persian`` even if **Farm Crop Formats** lagged.
        """
        if not stripped:
            return None
        sn = stripped.casefold()
        best = None
        for crop in sorted(CropInfo.objects.all(), key=lambda c: len(c.name), reverse=True):
            cn = crop.name.casefold()
            if sn.startswith(cn + " "):
                best = crop
                break
        if not best:
            return None
        remainder = stripped[len(best.name) :].strip()
        composed = compose_crop_sales_format_product_name(best.name, remainder)
        if composed.casefold() != sn:
            return None
        defaults = {
            "sale_price": Decimal("0.00"),
            "sale_unit": "lb",
            "harvest_qty_per_sale_unit": Decimal("1.00"),
            "sku": "",
            "is_active": True,
        }
        try:
            obj, created = CropSalesFormat.objects.update_or_create(
                crop=best, product_name=stripped, defaults=defaults
            )
            if created:
                self.stdout.write(
                    self.style.WARNING(
                        f"   ⚙  Auto-created CropSalesFormat for plan product '{stripped}' (crop='{best.name}')"
                    )
                )
            return obj
        except (ValueError, ValidationError, IntegrityError, DatabaseError):
            return None

    def _get_recipe_for_product(self, product, recipe_name, pack_date_year=None):
        """Resolve ProductRecipe for product + recipe name + planning calendar year."""
        if not recipe_name:
            return None
        product_pk = getattr(product, "pk", None)
        year_bucket = int(pack_date_year) if pack_date_year is not None else 0
        cache_key = (product_pk or 0, year_bucket, recipe_name)
        if cache_key in self.recipe_cache:
            return self.recipe_cache[cache_key]
        if product_pk and not self.write_disabled:
            qs = ProductRecipe.objects.filter(
                product=product, name=recipe_name, is_active=True
            )
            resolved = None
            if pack_date_year is not None:
                py = PlanningYear.objects.filter(year=pack_date_year).first()
                if py is not None:
                    resolved = qs.filter(planning_year=py).order_by("id").first()
            if resolved is None:
                resolved = qs.order_by("-planning_year__year", "-id").first()
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
        cache_key = (getattr(crop, "id", crop), block_type)
        if cache_key not in self.crop_season_cache:
            matches = list(
                CropBySeason.objects.filter(crop=crop, block_type=block_type).order_by("id")[:2]
            )
            crop_season = matches[0] if matches else None
            if crop_season and len(matches) > 1:
                self.stdout.write(
                    self.style.WARNING(
                        f"   ⚠  Multiple crop_season matches for {crop}/{block_type}; using id={crop_season.id}"
                    )
                )
            self.crop_season_cache[cache_key] = crop_season
        return self.crop_season_cache[cache_key]

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

    def _ensure_planning_year(self, year_int):
        """Return PlanningYear, creating it when missing (apply mode). Dry-run returns int year."""
        if year_int is None:
            return None
        cached = self.planning_year_cache.get(year_int)
        if cached is not None:
            return cached
        if self.write_disabled:
            self.planning_year_cache[year_int] = year_int
            return year_int
        obj, _ = PlanningYear.objects.get_or_create(
            year=year_int,
            defaults={
                "status": (
                    "archived" if year_int < date.today().year else "planning"
                ),
                "overplant_factor": Decimal("1.10"),
            },
        )
        self.planning_year_cache[year_int] = obj
        return obj

    def _parse_date_loose(self, date_str):
        """Parse common sheet/CSV date shapes; raises ValueError if unparseable."""
        if date_str is None:
            raise ValueError("empty date")
        s = str(date_str).strip()
        if not s:
            raise ValueError("empty date")
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Invalid date format: {date_str!r}")

    def _parse_date_optional(self, date_str):
        """Like `_parse_date_loose` but returns None for blanks / parse failures."""
        if date_str is None or not str(date_str).strip():
            return None
        try:
            return self._parse_date_loose(date_str)
        except ValueError:
            return None

    def _csv_decimal_allow_zero(self, raw):
        """Parse decimal; empty -> None; allows zero (unlike `_dec_or_none`)."""
        if raw is None or str(raw).strip() in ("", "na", "NA"):
            return None
        try:
            cleaned = str(raw).strip().replace("$", "").replace(",", "")
            return Decimal(cleaned)
        except (InvalidOperation, TypeError):
            return None

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

    def _bool_csv(self, value, default=False):
        """Parse spreadsheet-style booleans; missing/blank -> default."""
        s = str(value if value is not None else "").strip().lower()
        if not s:
            return default
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
        return default

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
            if self.strict_apply and total_error > 0:
                self.stdout.write(
                    self.style.ERROR(
                        "\n✗ Apply saved data but finished with row errors; command exits non-zero"
                    )
                )
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

    def _build_pack_skip_summary(self):
        """Group pack-scope skipped rows by model/code/field for operator triage."""
        grouped = {}
        for item in self.skip_reasons:
            key = (
                item.get("model"),
                item.get("code"),
                item.get("field_path"),
                item.get("message"),
            )
            if key not in grouped:
                grouped[key] = {
                    "model": item.get("model"),
                    "code": item.get("code"),
                    "field_path": item.get("field_path"),
                    "message": item.get("message"),
                    "count": 0,
                    "rows": [],
                }
            grouped[key]["count"] += 1
            grouped[key]["rows"].append(item.get("row"))
        summary = sorted(
            grouped.values(),
            key=lambda row: (
                row["model"] or "",
                row["code"] or "",
                row["field_path"] or "",
                row["message"] or "",
            ),
        )
        for row in summary:
            row["rows"] = sorted(set(row["rows"]))
        return summary

    def _aggregate_import_totals(self):
        """Roll up per-model counters into aggregate totals (canonical summary.totals)."""
        totals = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
        for model_name in sorted(self.stats.keys()):
            normalized = self._normalized_outcomes(self.stats[model_name])
            for key in totals:
                totals[key] += normalized[key]
        return totals

    def _enforce_reference_tier_csv_presence(self):
        """Fail apply when a Tier-1 reference CSV is absent while its target table is empty."""
        if not self.require_reference:
            return
        checks = (
            ("blocks.csv", Block.objects.exists),
            ("crop_info.csv", CropInfo.objects.exists),
            ("crop_by_season.csv", CropBySeason.objects.exists),
            ("sales_channels.csv", SalesChannel.objects.exists),
            ("crop_sales_formats.csv", CropSalesFormat.objects.exists),
            (
                "product_recipe_components.csv",
                lambda: ProductRecipe.objects.exists() or ProductRecipeComponent.objects.exists(),
            ),
            ("seed_sources.csv", Variety.objects.exists),
        )
        missing = []
        for filename, table_nonempty in checks:
            path = self._resolve_reference_path(filename)
            if os.path.exists(path) or table_nonempty():
                continue
            missing.append(filename)
        if missing:
            raise CommandError(
                "Tier-1 reference CSV(s) missing for empty database tables: "
                f"{', '.join(missing)} (under '{self.data_dir}'). "
                "Add the files or pass --no-require-reference for exceptional runs."
            )

    def _write_summary_json(self, status="ok", fatal_error=None):
        """Write structured summary artifact when requested."""
        per_model = {}
        for model_name in sorted(self.stats.keys()):
            normalized = self._normalized_outcomes(self.stats[model_name])
            per_model[model_name] = normalized
        totals = self._aggregate_import_totals()

        failure_signatures = self._build_failure_signatures(status, fatal_error)
        payload = {
            "schema_version": "1.5",
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
                "row_warnings": self.row_warnings,
                "pack_skip_rows": self.skip_reasons,
                "pack_skip_summary": self._build_pack_skip_summary(),
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
