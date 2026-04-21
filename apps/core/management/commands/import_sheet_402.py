"""Import sheet 402 seed sources and seed order stubs from CSV (see docs/prototype-build-backlog.md)."""

import csv
import os
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planning.models import PlanningYear, SeedOrder
from reference.models import CropInfo, Variety


class Command(BaseCommand):
    help = "Import Variety rows and SeedOrder stubs from a directory of CSV files."

    def add_arguments(self, parser):
        parser.add_argument("data_dir", type=str, help="Directory containing sheet402_*.csv files")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate without saving",
        )

    def handle(self, *args, **options):
        data_dir = options["data_dir"]
        dry_run = options["dry_run"]
        sources = os.path.join(data_dir, "sheet402_seed_sources.csv")
        orders = os.path.join(data_dir, "sheet402_seed_order.csv")
        if not os.path.isfile(sources):
            raise CommandError(f"Missing {sources}")
        if dry_run:
            self.stdout.write("DRY RUN — rolling back transaction\n")

        with transaction.atomic():
            self._import_sources(sources, dry_run)
            if os.path.isfile(orders):
                self._import_orders(orders, dry_run)
            if dry_run:
                transaction.set_rollback(True)

    def _import_sources(self, path: str, dry_run: bool) -> None:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            required = {"Crop", "Variety"}
            if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
                raise CommandError(f"{path} must include columns: {sorted(required)}")
            for row in reader:
                crop_name = (row.get("Crop") or "").strip()
                variety_name = (row.get("Variety") or "").strip()
                if not crop_name or not variety_name:
                    continue
                crop = CropInfo.objects.filter(name=crop_name).first()
                if not crop:
                    self.stdout.write(self.style.WARNING(f"Skip unknown crop: {crop_name}"))
                    continue
                if dry_run:
                    self.stdout.write(f"Would upsert variety {crop_name} / {variety_name}")
                    continue
                Variety.objects.update_or_create(
                    crop=crop,
                    name=variety_name,
                    defaults={
                        "supplier": (row.get("Supplier") or "").strip(),
                        "catalog_number": (row.get("Catalog Number") or row.get("Catalog") or "").strip(),
                        "source_url": (row.get("Source URL") or row.get("URL") or "").strip()[:500],
                        "notes": (row.get("Notes") or "").strip(),
                    },
                )

    def _import_orders(self, path: str, dry_run: bool) -> None:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            required = {"Crop", "Variety", "Season Year", "Planned Quantity", "Unit"}
            if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
                raise CommandError(f"{path} must include columns: {sorted(required)}")
            for row in reader:
                crop_name = (row.get("Crop") or "").strip()
                variety_name = (row.get("Variety") or "").strip()
                try:
                    year = int((row.get("Season Year") or "").strip())
                except ValueError:
                    continue
                py = PlanningYear.objects.filter(year=year).first()
                if not py:
                    self.stdout.write(self.style.WARNING(f"Skip unknown planning year {year}"))
                    continue
                crop = CropInfo.objects.filter(name=crop_name).first()
                if not crop:
                    continue
                variety = Variety.objects.filter(crop=crop, name=variety_name).first()
                if not variety:
                    self.stdout.write(
                        self.style.WARNING(f"Skip order — create variety first: {crop_name} / {variety_name}")
                    )
                    continue
                try:
                    qty = Decimal(str(row.get("Planned Quantity") or "0").strip())
                except (InvalidOperation, TypeError):
                    continue
                unit = (row.get("Unit") or "ounces").strip()[:20]
                notes = (row.get("Notes") or "").strip()
                if dry_run:
                    self.stdout.write(f"Would upsert seed order {variety} {year} {qty} {unit}")
                    continue
                SeedOrder.objects.update_or_create(
                    variety=variety,
                    planning_year=py,
                    defaults={"planned_quantity": qty, "unit": unit, "notes": notes},
                )
