"""core/management/commands/export_season.py"""

import csv
import os
import json
from datetime import date
from django.core.management.base import BaseCommand
from planning.models import PlanningYear, Planting, NurseryEvent, HarvestEvent
from operations.models import FieldWalkNote, InventoryLedger
from sales.models import SalesEvent, QuickSalesEntry
from reference.models import CropInfo, CropBySeason, Block, SalesChannel


class Command(BaseCommand):
    help = "Export all data for a planning year to CSV and JSON"

    def add_arguments(self, parser):
        parser.add_argument("year", type=int)
        parser.add_argument("output_dir", type=str)

    def handle(self, *args, **options):
        year = options["year"]
        output_dir = options["output_dir"]
        os.makedirs(output_dir, exist_ok=True)

        try:
            year_obj = PlanningYear.objects.get(year=year)
        except PlanningYear.DoesNotExist:
            self.stderr.write(f"Planning year {year} not found.\n")
            return

        self.stdout.write(f"Exporting {year} season data to {output_dir}/\n")

        # Reference data (always export — it's the baseline)
        self._export_model(
            CropInfo.objects.all(),
            [
                "name",
                "crop_type",
                "botanical_family",
                "propagation_type",
                "is_perennial",
                "fresh_or_storage",
                "storage_weeks",
                "can_hold_in_field",
                "harvest_unit",
                "avg_unit_weight",
                "units_per_bin",
                "harvest_bin",
                "harvest_tools",
                "harvest_rate_per_hour",
                "nursery_weeks",
                "weeks_until_pot_up",
                "pot_up_tray_size",
                "seeded_tray_size",
                "seeds_per_cell",
                "thinned_plants",
                "seeds_per_ounce",
            ],
            os.path.join(output_dir, "crop_info.csv"),
        )

        self._export_model(
            Block.objects.all(),
            [
                "name",
                "block_type",
                "num_beds",
                "bed_width_feet",
                "bedfeet_per_bed",
                "walk_route_order",
            ],
            os.path.join(output_dir, "blocks.csv"),
        )

        self._export_model(
            SalesChannel.objects.all(),
            [
                "name",
                "days_of_week",
                "start_week",
                "end_week",
                "weekly_target",
                "is_csa",
                "allocation_priority",
            ],
            os.path.join(output_dir, "sales_channels.csv"),
        )

        # Season-specific data
        plantings = Planting.objects.filter(planning_year=year_obj).select_related(
            "crop", "crop_season", "block"
        )

        self._export_plantings(
            plantings,
            os.path.join(output_dir, "plantings.csv"),
        )

        self._export_model(
            NurseryEvent.objects.filter(planting__planning_year=year_obj),
            [
                "id",
                "planting_id",
                "event_type",
                "planned_date",
                "planned_tray_count",
                "planned_tray_size",
                "actual_date",
                "actual_tray_count",
                "actual_tray_size",
                "actual_germination_rate",
                "notes",
            ],
            os.path.join(output_dir, "nursery_events.csv"),
        )

        self._export_model(
            HarvestEvent.objects.filter(planting__planning_year=year_obj),
            [
                "id",
                "planting_id",
                "planned_date",
                "planned_quantity",
                "planned_units",
                "actual_date",
                "actual_quantity",
                "actual_units",
                "actual_bins",
                "actual_bin_type",
                "actual_hours",
                "actual_workers",
                "quality_grade",
                "notes",
            ],
            os.path.join(output_dir, "harvest_events.csv"),
        )

        self._export_model(
            FieldWalkNote.objects.filter(planting__planning_year=year_obj),
            [
                "id",
                "planting_id",
                "walk_date",
                "condition",
                "adjusted_first_harvest_date",
                "adjusted_last_harvest_date",
                "yield_adjust_pct",
                "notes",
            ],
            os.path.join(output_dir, "field_walk_notes.csv"),
        )

        self._export_model(
            SalesEvent.objects.filter(sale_date__year=year),
            [
                "id",
                "channel_id",
                "sale_date",
                "product_id",
                "planned_quantity",
                "planned_revenue",
                "actual_quantity",
                "actual_revenue",
                "actual_price",
                "brought_quantity",
                "returned_quantity",
                "notes",
            ],
            os.path.join(output_dir, "sales_events.csv"),
        )

        self._export_model(
            QuickSalesEntry.objects.filter(sale_date__year=year),
            ["id", "channel_id", "sale_date", "total_cash", "total_card", "notes"],
            os.path.join(output_dir, "quick_sales.csv"),
        )

        self._export_model(
            InventoryLedger.objects.filter(event_date__year__in=[year, year + 1]),
            [
                "id",
                "crop_id",
                "harvest_event_id",
                "event_date",
                "event_type",
                "quantity",
                "running_balance",
                "expiry_date",
                "storage_location",
                "notes",
            ],
            os.path.join(output_dir, "inventory_ledger.csv"),
        )

        # Full JSON archive
        self._export_json_archive(year_obj, output_dir)

        self.stdout.write(self.style.SUCCESS(f"Export complete: {output_dir}/\n"))

    def _export_plantings(self, plantings, path):
        """Custom export for plantings with denormalized crop/block names."""
        fields = [
            "id",
            "crop_name",
            "crop_type",
            "botanical_family",
            "variety",
            "block_name",
            "block_type",
            "bed_start",
            "bed_end",
            "succession_group",
            "planned_bedfeet",
            "planned_plant_date",
            "planned_first_harvest_date",
            "planned_last_harvest_date",
            "planned_total_yield",
            "actual_bedfeet",
            "actual_plant_date",
            "actual_first_harvest_date",
            "actual_last_harvest_date",
            "actual_total_yield",
            "status",
            "revision_of_id",
            "notes",
        ]

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

            for p in plantings:
                writer.writerow(
                    {
                        "id": p.id,
                        "crop_name": p.crop.name,
                        "crop_type": p.crop.crop_type,
                        "botanical_family": p.crop.botanical_family,
                        "variety": p.variety,
                        "block_name": p.block.name,
                        "block_type": p.block.block_type,
                        "bed_start": p.bed_start,
                        "bed_end": p.bed_end,
                        "succession_group": p.succession_group,
                        "planned_bedfeet": p.planned_bedfeet,
                        "planned_plant_date": p.planned_plant_date,
                        "planned_first_harvest_date": p.planned_first_harvest_date,
                        "planned_last_harvest_date": p.planned_last_harvest_date,
                        "planned_total_yield": p.planned_total_yield,
                        "actual_bedfeet": p.actual_bedfeet,
                        "actual_plant_date": p.actual_plant_date,
                        "actual_first_harvest_date": p.actual_first_harvest_date,
                        "actual_last_harvest_date": p.actual_last_harvest_date,
                        "actual_total_yield": p.actual_total_yield,
                        "status": p.status,
                        "revision_of_id": p.revision_of_id,
                        "notes": p.notes,
                    }
                )

        count = plantings.count()
        self.stdout.write(f"  {path}: {count} plantings\n")

    def _export_model(self, queryset, fields, path):
        """Generic CSV export for a queryset."""
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

            count = 0
            for obj in queryset:
                row = {}
                for field in fields:
                    val = getattr(obj, field, None)
                    if callable(val):
                        val = val()
                    row[field] = val
                writer.writerow(row)
                count += 1

        self.stdout.write(f"  {path}: {count} records\n")

    def _export_json_archive(self, year_obj, output_dir):
        """Full season archive as a single JSON file."""
        from django.core.serializers import serialize

        year = year_obj.year

        archive = {
            "meta": {
                "year": year,
                "exported": date.today().isoformat(),
                "status": year_obj.status,
            },
            "reference": {
                "crops": json.loads(serialize("json", CropInfo.objects.all())),
                "crop_by_season": json.loads(serialize("json", CropBySeason.objects.all())),
                "blocks": json.loads(serialize("json", Block.objects.all())),
                "channels": json.loads(serialize("json", SalesChannel.objects.all())),
            },
            "season": {
                "plantings": json.loads(
                    serialize("json", Planting.objects.filter(planning_year=year_obj))
                ),
                "nursery_events": json.loads(
                    serialize("json", NurseryEvent.objects.filter(planting__planning_year=year_obj))
                ),
                "harvest_events": json.loads(
                    serialize("json", HarvestEvent.objects.filter(planting__planning_year=year_obj))
                ),
                "field_walk_notes": json.loads(
                    serialize(
                        "json", FieldWalkNote.objects.filter(planting__planning_year=year_obj)
                    )
                ),
                "sales_events": json.loads(
                    serialize("json", SalesEvent.objects.filter(sale_date__year=year))
                ),
                "inventory_ledger": json.loads(
                    serialize(
                        "json",
                        InventoryLedger.objects.filter(event_date__year__in=[year, year + 1]),
                    )
                ),
            },
        }

        path = os.path.join(output_dir, f"season_{year}_archive.json")
        with open(path, "w") as f:
            json.dump(archive, f, indent=2, default=str)

        self.stdout.write(f"  {path}: full archive\n")
