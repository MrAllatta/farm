"""Tests for import_sheet_402 management command."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from planning.models import PlanningYear, SeedOrder
from reference.models import CropInfo, Variety


class ImportSheet402CommandTests(TestCase):
    def setUp(self):
        self.year = PlanningYear.objects.create(year=4020, status="active")
        self.crop = CropInfo.objects.create(
            name="Sheet402Crop",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="bunch",
            avg_unit_weight=Decimal("0.50"),
            nursery_weeks=0,
        )

    def test_dry_run_does_not_create_varieties(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "sheet402_seed_sources.csv"
            p.write_text(
                "Crop,Variety,Supplier\n"
                "Sheet402Crop,Test Variety X,ACME\n"
                "UnknownCrop,VV,ACME\n",
                encoding="utf-8",
            )
            call_command("import_sheet_402", tmp, "--dry-run")
        self.assertEqual(Variety.objects.filter(name="Test Variety X").count(), 0)

    def test_apply_creates_variety_and_seed_order(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "sheet402_seed_sources.csv").write_text(
                "Crop,Variety,Supplier\nSheet402Crop,Apply Variety,ACME\n",
                encoding="utf-8",
            )
            (d / "sheet402_seed_order.csv").write_text(
                "Crop,Variety,Season Year,Planned Quantity,Unit\n"
                f"Sheet402Crop,Apply Variety,{self.year.year},3.5,ounces\n",
                encoding="utf-8",
            )
            call_command("import_sheet_402", str(d))
        v = Variety.objects.get(crop=self.crop, name="Apply Variety")
        self.assertEqual(v.supplier, "ACME")
        so = SeedOrder.objects.get(variety=v, planning_year=self.year)
        self.assertEqual(so.planned_quantity, Decimal("3.50"))

    def test_seed_order_creates_variety_without_seed_sources_row(self):
        """402 'Seed Order' alone should still populate Variety for each ordered line."""
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "sheet402_seed_sources.csv").write_text(
                "Crop,Variety,Supplier\n",
                encoding="utf-8",
            )
            (d / "sheet402_seed_order.csv").write_text(
                "Crop,Variety,Season Year,Planned Quantity,Unit,Notes\n"
                f"Sheet402Crop,Order Only Variety,{self.year.year},2,packets,order line note\n",
                encoding="utf-8",
            )
            call_command("import_sheet_402", str(d))
        v = Variety.objects.get(crop=self.crop, name="Order Only Variety")
        self.assertEqual(v.supplier, "")
        self.assertEqual(v.notes, "")
        so = SeedOrder.objects.get(variety=v, planning_year=self.year)
        self.assertEqual(so.notes, "order line note")
        self.assertEqual(so.planned_quantity, Decimal("2"))
