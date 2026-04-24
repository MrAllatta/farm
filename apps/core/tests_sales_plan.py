import json
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from sales.models import SalesEvent


class ProductWeekPlanImportTests(TestCase):
    def _write_csv(self, root: Path, name: str, lines: list[str]) -> None:
        root.joinpath(name).write_text("\n".join(lines), encoding="utf-8")

    def test_import_historical_data_imports_product_week_plan_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            year_dir = data_dir / "year_2026"
            year_dir.mkdir(parents=True)

            self._write_csv(
                data_dir,
                "blocks.csv",
                [
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed",
                    "Field 1,Field,10,3,100",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_info.csv",
                [
                    "Crop,Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Units,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Size,Seeded Tray Size,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Carrot,Vegetables,Apiaceae,Fresh,0,pounds,1,,,,,0,0,,,1,0,",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_by_season.csv",
                [
                    "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                    "Carrot,Field,10,40,1.2,6,65,3,30,na,,,,,",
                ],
            )
            self._write_csv(
                data_dir,
                "sales_channels.csv",
                [
                    "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week,is_csa,Priority",
                    "Farm Stand,Saturday,1,52,500,false,1",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_sales_formats.csv",
                [
                    "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                    "Carrot,Carrot Bunch,3.50,bunch,1,CAR-BUN,true",
                ],
            )
            self._write_csv(
                data_dir,
                "product_recipe_components.csv",
                [
                    "Mix Product Name,Mix Crop Name,Component Source Type,"
                    "Component Crop Name,Component Percent,Recipe Name",
                ],
            )
            self._write_csv(
                data_dir,
                "seed_sources.csv",
                ["Crop,Variety,Supplier,Catalog Number,Source URL,Notes"],
            )
            self._write_csv(
                year_dir,
                "planning_year.csv",
                [
                    "Year,Status,Overplant Factor",
                    "2026,planning,1.10",
                ],
            )
            self._write_csv(
                year_dir,
                "product_week_plan.csv",
                [
                    "Channel Name,Product Name,Week,Planned Quantity,Planned Revenue,Notes",
                    "Farm Stand,Carrot Bunch,12,11,38.5,seed demand",
                ],
            )

            call_command(
                "import_historical_data",
                str(data_dir),
                "--start-year",
                "2026",
                "--end-year",
                "2026",
            )

        self.assertTrue(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                product__product_name="Carrot Bunch",
                channel__name="Farm Stand",
            ).exists()
        )

    def test_import_historical_data_product_week_invalid_week_row_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            year_dir = data_dir / "year_2026"
            year_dir.mkdir(parents=True)
            summary_path = data_dir / "summary.json"

            self._write_csv(
                data_dir,
                "blocks.csv",
                [
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed",
                    "Field 1,Field,10,3,100",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_info.csv",
                [
                    "Crop,Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Units,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Size,Seeded Tray Size,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Carrot,Vegetables,Apiaceae,Fresh,0,pounds,1,,,,,0,0,,,1,0,",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_by_season.csv",
                [
                    "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                    "Carrot,Field,10,40,1.2,6,65,3,30,na,,,,,",
                ],
            )
            self._write_csv(
                data_dir,
                "sales_channels.csv",
                [
                    "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week,is_csa,Priority",
                    "Farm Stand,Saturday,1,52,500,false,1",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_sales_formats.csv",
                [
                    "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                    "Carrot,Carrot Bunch,3.50,bunch,1,CAR-BUN,true",
                ],
            )
            self._write_csv(
                year_dir,
                "planning_year.csv",
                [
                    "Year,Status,Overplant Factor",
                    "2026,planning,1.10",
                ],
            )
            self._write_csv(
                year_dir,
                "product_week_plan.csv",
                [
                    "Channel Name,Product Name,Week,Planned Quantity,Planned Revenue,Notes",
                    "Farm Stand,Carrot Bunch,99,1,0,bad week",
                ],
            )

            call_command(
                "import_historical_data",
                str(data_dir),
                "--start-year",
                "2026",
                "--end-year",
                "2026",
                "--validate-only",
                "--summary-json",
                str(summary_path),
            )

            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            row_errors = payload["results"]["row_errors"]
            self.assertEqual(len(row_errors), 1)
            self.assertEqual(row_errors[0]["code"], "namespace_mismatch")
            self.assertIn("week", row_errors[0]["field_path"])


class SalesPlan302ImportTests(TestCase):
    """Workbook 302: category-level plan rows (``SalesEvent.sales_category``, no channel)."""

    def _write_csv(self, root, name: str, lines: list[str]) -> None:
        root.joinpath(name).write_text("\n".join(lines), encoding="utf-8")

    def test_import_sales_plan_302_csv(self):
        from datetime import datetime

        from reference.models import SalesCategory

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            year_dir = data_dir / "year_2026"
            year_dir.mkdir(parents=True)

            self._write_csv(
                data_dir,
                "blocks.csv",
                [
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed",
                    "Field 1,Field,10,3,100",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_info.csv",
                [
                    "Crop,Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Units,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Size,Seeded Tray Size,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Carrot,Vegetables,Apiaceae,Fresh,0,pounds,1,,,,,0,0,,,1,0,",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_by_season.csv",
                [
                    "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                    "Carrot,Field,10,40,1.2,6,65,3,30,na,,,,,",
                ],
            )
            self._write_csv(
                data_dir,
                "sales_channels.csv",
                [
                    "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week,is_csa,Priority",
                    "Farm Stand,Saturday,1,52,500,false,1",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_sales_formats.csv",
                [
                    "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                    "Carrot,Carrot Bunch,3.50,bunch,1,CAR-BUN,true",
                ],
            )
            self._write_csv(
                data_dir,
                "product_recipe_components.csv",
                [
                    "Mix Product Name,Mix Crop Name,Component Source Type,"
                    "Component Crop Name,Component Percent,Recipe Name",
                ],
            )
            self._write_csv(
                data_dir,
                "seed_sources.csv",
                ["Crop,Variety,Supplier,Catalog Number,Source URL,Notes"],
            )
            self._write_csv(
                year_dir,
                "planning_year.csv",
                [
                    "Year,Status,Overplant Factor",
                    "2026,planning,1.10",
                ],
            )
            self._write_csv(
                year_dir,
                "sales_plan_302.csv",
                [
                    "Channel,Product,Harvest Year,Harvest Week,Qty,Value",
                    "Markets,Carrot Bunch,2026,12,10,35",
                ],
            )

            call_command(
                "import_historical_data",
                str(data_dir),
                "--start-year",
                "2026",
                "--end-year",
                "2026",
            )

        cat = SalesCategory.objects.get(name=SalesCategory.CategoryName.MARKETS)
        ev = SalesEvent.objects.get(
            entry_kind=SalesEvent.EntryKind.PLAN,
            sales_category=cat,
            channel__isnull=True,
            product__product_name="Carrot Bunch",
        )
        self.assertEqual(ev.planned_quantity, Decimal("10"))
        self.assertEqual(ev.planned_revenue, Decimal("35"))
        self.assertEqual(ev.sale_date, datetime.fromisocalendar(2026, 12, 1).date())
