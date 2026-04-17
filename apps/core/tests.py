import csv
import json
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import SystemCheckError
from django.core.management import call_command
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone
from django.urls import get_resolver, reverse

from operations.models import FieldWalkNote, InventoryLedger, PackBatch
from core.models import RotationHistory
from core.planning_year import resolve_current_planning_year
from core.google_sheets_connector import extract_drive_folder_id, extract_spreadsheet_id
from planning.models import HarvestEvent, NurseryEvent, Planting, PlanningYear
from sales.models import QuickSalesEntry, SalesEvent
from reference.models import (
    Block,
    CropBySeason,
    CropInfo,
    CropSalesFormat,
    ProductRecipe,
    SalesChannel,
)
from core.spreadsheet_connector import normalize_rows


class ImportHistoricalDataCommandTests(TestCase):
    SUMMARY_TOP_LEVEL_KEYS = {"schema_version", "status", "fatal_error", "run", "results"}
    SUMMARY_RUN_KEYS = {
        "run_id",
        "started_at",
        "finished_at",
        "data_dir",
        "start_year",
        "end_year",
        "validate_only",
        "dry_run",
        "atomic_apply",
        "verbose",
    }
    SUMMARY_RESULTS_KEYS = {
        "models",
        "totals",
        "row_errors",
        "failure_signatures",
        "escalation_summary",
    }
    MODEL_TOTAL_KEYS = {"created", "updated", "skipped", "error"}
    ROW_ERROR_KEYS = {"model", "row", "code", "field_path", "message"}
    FAILURE_SIGNATURE_KEYS = {
        "signature",
        "count",
        "owner_area",
        "owner_team",
        "severity",
        "escalation_path",
        "recovery",
        "example",
    }
    ESCALATION_SUMMARY_KEYS = {
        "owner_area",
        "owner_team",
        "severity",
        "escalation_path",
        "count",
        "signatures",
        "recovery_steps",
    }

    def _write_csv(self, data_dir, name, lines):
        Path(data_dir, name).write_text("\n".join(lines), encoding="utf-8")

    def _write_clean_fixture(self, data_dir):
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

    def _write_known_mismatch_fixture(self, data_dir):
        self._write_clean_fixture(data_dir)
        self._write_csv(
            data_dir,
            "crop_by_season.csv",
            [
                "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                "Carrot,Unknown Block,10,40,1.2,6,65,3,30,na,,,,,",
                "Ghost Crop,Field,10,40,1.2,6,65,3,30,na,,,,,",
                "Carrot,Field,10,40,1.2,6,0,3,30,na,,,,,",
            ],
        )
        self._write_csv(
            data_dir,
            "crop_sales_formats.csv",
            [
                "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                "Carrot,Carrot Bunch,3.50,bunch,1,CAR-BUN,true",
                "Ghost Crop,Ghost Bunch,4.00,bunch,1,GHO-BUN,true",
            ],
        )

    def _write_block_type_normalization_fixture(self, data_dir):
        self._write_clean_fixture(data_dir)
        self._write_csv(
            data_dir,
            "crop_by_season.csv",
            [
                "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                "Carrot,  high    tunnel  ,10,40,1.2,6,65,3,30,na,,,,,",
            ],
        )

    def _write_edge_case_fixture(self, data_dir):
        self._write_clean_fixture(data_dir)
        self._write_csv(
            data_dir,
            "crop_by_season.csv",
            [
                "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                "Carrot,Field,10,40,1.2,6,0,3,30,na,,,,,",
                "Carrot,Unknown Block,10,40,1.2,6,65,3,30,na,,,,,",
            ],
        )
        self._write_csv(
            data_dir,
            "crop_sales_formats.csv",
            [
                "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                "Carrot,Carrot Bunch,3.50,bunch,1,CAR-BUN,true",
                "Ghost Crop,Ghost Bunch,4.00,bunch,1,GHO-BUN,true",
            ],
        )

    def _write_year_fixture(self, data_dir, year=2021):
        year_dir = Path(data_dir) / f"year_{year}"
        year_dir.mkdir(parents=True, exist_ok=True)
        self._write_csv(
            year_dir,
            "planning_year.csv",
            [
                "Year,Status,Overplant Factor",
                f"{year},planning,1.10",
            ],
        )
        self._write_csv(
            year_dir,
            "plantings.csv",
            [
                "ID,Crop Name,Block Name,Variety,Bed Start,Bed End,Planned Bedfeet,Planned Plant Date,Status",
                "P1,Carrot,Field 1,Nantes,1,1,100,2021-04-01,Planned",
            ],
        )
        self._write_csv(
            year_dir,
            "sales_events.csv",
            [
                "Channel Name,Sale Date,Product Name,Planned Quantity,Planned Revenue,Actual Quantity,Actual Revenue,Actual Price,Brought Quantity,Returned Quantity,Notes",
                f"  FARM   STAND  ,{year}-06-01,  CARROT   BUNCH  ,10,35,9,31.5,3.5,10,1,normalized lookup test",
            ],
        )
        self._write_csv(
            year_dir,
            "pack_allocations.csv",
            [
                "Planting ID,Harvest Date,Channel,Product,Pack Date,Quantity,Notes",
                f"P1,, farm stand , carrot bunch ,{year}-06-02,5,duplicate-safe lookup test",
            ],
        )

    def _write_partial_year_fixture(self, data_dir, years):
        self._write_clean_fixture(data_dir)
        for year in years:
            self._write_year_fixture(data_dir, year=year)

    def _write_ops_sales_error_fixture(self, data_dir, year=2021):
        year_dir = Path(data_dir) / f"year_{year}"
        year_dir.mkdir(parents=True, exist_ok=True)
        self._write_csv(
            year_dir,
            "inventory_ledger.csv",
            [
                "Crop Name,Event Date,Event Type,Quantity,Storage Location,Notes",
                ",2021-06-01,Return In,2,Barn,missing crop",
                "Ghost Crop,2021-06-02,Return In,3,Barn,stale crop",
                "Carrot,,Return In,4,Barn,missing event date",
            ],
        )
        self._write_csv(
            year_dir,
            "pack_allocations.csv",
            [
                "Planting ID,Harvest Date,Channel,Product,Pack Date,Quantity,Notes",
                "P1,,Farm Stand,,2021-06-03,2,missing product",
                "P1,,Ghost Channel,Carrot Bunch,2021-06-03,2,stale channel",
                "P1,,Farm Stand,Ghost Product,2021-06-03,2,stale product",
            ],
        )
        self._write_csv(
            year_dir,
            "sales_events.csv",
            [
                "Channel Name,Sale Date,Product Name,Planned Quantity,Planned Revenue,Actual Quantity,Actual Revenue,Actual Price,Brought Quantity,Returned Quantity,Notes",
                "Farm Stand,,Carrot Bunch,1,3.5,1,3.5,3.5,1,0,missing sale date",
                "Ghost Channel,2021-06-04,Carrot Bunch,1,3.5,1,3.5,3.5,1,0,stale channel",
                "Farm Stand,2021-06-04,Ghost Product,1,3.5,1,3.5,3.5,1,0,stale product",
            ],
        )
        self._write_csv(
            year_dir,
            "quick_sales_entries.csv",
            [
                "Channel Name,Sale Date,Total Cash,Total Card,Notes",
                ",2021-06-05,10,0,missing channel",
                "Ghost Channel,2021-06-05,12,0,stale channel",
            ],
        )

    def _write_mixed_batch_fixture(self, data_dir, year=2021):
        self._write_clean_fixture(data_dir)
        self._write_year_fixture(data_dir, year=year)
        year_dir = Path(data_dir) / f"year_{year}"
        self._write_csv(
            year_dir,
            "inventory_ledger.csv",
            [
                "Crop Name,Event Date,Event Type,Quantity,Storage Location,Notes",
                "Carrot,2021-06-01,Return In,5,Barn,valid inventory row",
                ",2021-06-02,Return In,2,Barn,missing crop",
                "Ghost Crop,2021-06-03,Return In,3,Barn,stale crop",
                "Carrot,,Return In,4,Barn,missing event date",
            ],
        )
        self._write_csv(
            year_dir,
            "pack_allocations.csv",
            [
                "Planting ID,Harvest Date,Channel,Product,Pack Date,Quantity,Notes",
                "P1,,Farm Stand,Carrot Bunch,2021-06-03,2,valid pack allocation",
                "P1,,Farm Stand,,2021-06-03,2,missing product",
                "P1,,Ghost Channel,Carrot Bunch,2021-06-03,2,stale channel",
                "P1,,Farm Stand,Ghost Product,2021-06-03,2,stale product",
            ],
        )
        self._write_csv(
            year_dir,
            "sales_events.csv",
            [
                "Channel Name,Sale Date,Product Name,Planned Quantity,Planned Revenue,Actual Quantity,Actual Revenue,Actual Price,Brought Quantity,Returned Quantity,Notes",
                "Farm Stand,2021-06-04,Carrot Bunch,1,3.5,1,3.5,3.5,1,0,valid sales event",
                "Farm Stand,,Carrot Bunch,1,3.5,1,3.5,3.5,1,0,missing sale date",
                "Ghost Channel,2021-06-04,Carrot Bunch,1,3.5,1,3.5,3.5,1,0,stale channel",
                "Farm Stand,2021-06-04,Ghost Product,1,3.5,1,3.5,3.5,1,0,stale product",
            ],
        )
        self._write_csv(
            year_dir,
            "quick_sales_entries.csv",
            [
                "Channel Name,Sale Date,Total Cash,Total Card,Notes",
                "Farm Stand,2021-06-05,10,5,valid quick sale",
                ",2021-06-05,10,0,missing channel",
                "Ghost Channel,2021-06-05,12,0,stale channel",
            ],
        )

    def _write_mix_recipe_pack_fixture(
        self,
        data_dir,
        year=2021,
        recipe_name="Carrot Mix Recipe",
        packed_quantity="10",
        packed_unit="bag",
        pack_date=None,
    ):
        self._write_clean_fixture(data_dir)
        self._write_year_fixture(data_dir, year=year)
        year_dir = Path(data_dir) / f"year_{year}"
        if pack_date is None:
            pack_date = f"{year}-06-01"
        self._write_csv(
            year_dir,
            "pack_allocations.csv",
            [
                "Planting ID,Harvest Date,Channel,Product,Pack Date,Quantity,Recipe Name,Packed Quantity,Packed Unit,Notes",
                f"P1,,Farm Stand,Carrot Bunch,{pack_date},5,{recipe_name},{packed_quantity},{packed_unit},mix pack import test",
            ],
        )

    def _run_import(self, data_dir, summary_path, *extra_args):
        call_command("import_historical_data", data_dir, "--summary-json", str(summary_path), *extra_args)
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def _run_import_with_output(self, data_dir, summary_path, *extra_args):
        stdout = StringIO()
        stderr = StringIO()
        call_command(
            "import_historical_data",
            data_dir,
            "--summary-json",
            str(summary_path),
            *extra_args,
            stdout=stdout,
            stderr=stderr,
        )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return summary, stdout.getvalue(), stderr.getvalue()

    def _assert_summary_contract(
        self,
        summary,
        expected_validate_only,
        expected_dry_run=False,
        expected_atomic_apply=None,
    ):
        self.assertEqual(set(summary.keys()), self.SUMMARY_TOP_LEVEL_KEYS)
        self.assertEqual(summary["schema_version"], "1.3")
        self.assertIn(summary["status"], {"ok", "failed"})
        self.assertIn("fatal_error", summary)
        self.assertEqual(set(summary["run"].keys()), self.SUMMARY_RUN_KEYS)
        self.assertTrue(summary["run"]["run_id"])
        self.assertTrue(summary["run"]["started_at"])
        self.assertTrue(summary["run"]["finished_at"])
        self.assertEqual(summary["run"]["validate_only"], expected_validate_only)
        self.assertEqual(summary["run"]["dry_run"], expected_dry_run)
        if expected_atomic_apply is None:
            expected_atomic_apply = expected_validate_only or not expected_dry_run
        self.assertEqual(summary["run"]["atomic_apply"], expected_atomic_apply)
        self.assertEqual(set(summary["results"].keys()), self.SUMMARY_RESULTS_KEYS)
        self.assertEqual(set(summary["results"]["totals"].keys()), self.MODEL_TOTAL_KEYS)
        self.assertIsInstance(summary["results"]["row_errors"], list)
        self.assertIsInstance(summary["results"]["failure_signatures"], list)
        self.assertIsInstance(summary["results"]["escalation_summary"], list)
        self._assert_row_error_payload_contract(summary["results"]["row_errors"])
        self._assert_failure_signature_payload_contract(summary["results"]["failure_signatures"])
        self._assert_escalation_summary_payload_contract(summary["results"]["escalation_summary"])

        model_totals = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
        for model_counts in summary["results"]["models"].values():
            self.assertEqual(set(model_counts.keys()), {"created", "updated", "skipped", "error"})
            for key in model_totals:
                model_totals[key] += model_counts[key]
        self.assertEqual(summary["results"]["totals"], model_totals)

    def _assert_row_error_payload_contract(self, row_errors):
        expected_sorted_keys = ["code", "field_path", "message", "model", "row"]
        for item in row_errors:
            self.assertEqual(set(item.keys()), self.ROW_ERROR_KEYS)
            self.assertEqual(sorted(item.keys()), expected_sorted_keys)
            self.assertTrue(isinstance(item["model"], str) and item["model"])
            self.assertIsInstance(item["row"], int)
            self.assertGreater(item["row"], 0)
            self.assertTrue(isinstance(item["code"], str) and item["code"])
            self.assertTrue(isinstance(item["field_path"], str) and item["field_path"])
            self.assertTrue(isinstance(item["message"], str) and item["message"])
            self.assertEqual(item["model"], item["model"].strip())
            self.assertEqual(item["code"], item["code"].strip())
            self.assertEqual(item["field_path"], item["field_path"].strip())

    def _assert_deterministic_row_errors(self, row_errors, expected_entries):
        self._assert_row_error_payload_contract(row_errors)
        self.assertEqual(
            [
                (
                    item["model"],
                    item["row"],
                    item["code"],
                    item["field_path"],
                    item["message"],
                )
                for item in row_errors
            ],
            expected_entries,
        )

    def _assert_failure_signature_payload_contract(self, failure_signatures):
        example_keys = {"model", "field_path", "message"}
        for item in failure_signatures:
            self.assertEqual(set(item.keys()), self.FAILURE_SIGNATURE_KEYS)
            self.assertTrue(isinstance(item["signature"], str) and item["signature"])
            self.assertIsInstance(item["count"], int)
            self.assertGreater(item["count"], 0)
            self.assertTrue(isinstance(item["owner_area"], str) and item["owner_area"])
            self.assertTrue(isinstance(item["owner_team"], str) and item["owner_team"])
            self.assertIn(item["severity"], {"high", "medium"})
            self.assertTrue(isinstance(item["escalation_path"], str) and item["escalation_path"])
            self.assertTrue(isinstance(item["recovery"], str) and item["recovery"])
            self.assertEqual(set(item["example"].keys()), example_keys)
            self.assertTrue(isinstance(item["example"]["model"], str) and item["example"]["model"])
            self.assertTrue(
                isinstance(item["example"]["field_path"], str) and item["example"]["field_path"]
            )
            self.assertTrue(isinstance(item["example"]["message"], str) and item["example"]["message"])

    def _assert_escalation_summary_payload_contract(self, escalation_summary):
        for item in escalation_summary:
            self.assertEqual(set(item.keys()), self.ESCALATION_SUMMARY_KEYS)
            self.assertTrue(isinstance(item["owner_area"], str) and item["owner_area"])
            self.assertTrue(isinstance(item["owner_team"], str) and item["owner_team"])
            self.assertIn(item["severity"], {"high", "medium"})
            self.assertTrue(isinstance(item["escalation_path"], str) and item["escalation_path"])
            self.assertIsInstance(item["count"], int)
            self.assertGreater(item["count"], 0)
            self.assertIsInstance(item["signatures"], list)
            self.assertTrue(item["signatures"])
            self.assertEqual(item["signatures"], sorted(item["signatures"]))
            for signature in item["signatures"]:
                self.assertTrue(isinstance(signature, str) and signature)
            self.assertIsInstance(item["recovery_steps"], list)
            self.assertTrue(item["recovery_steps"])
            self.assertEqual(item["recovery_steps"], sorted(item["recovery_steps"]))
            for recovery_step in item["recovery_steps"]:
                self.assertTrue(isinstance(recovery_step, str) and recovery_step)

    def test_clean_fixture_validate_only_has_no_writes_and_canonical_outcomes(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            summary_path = Path(output_dir) / "summary.json"
            summary = self._run_import(data_dir, summary_path, "--validate-only")

            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
            self.assertEqual(Block.objects.count(), 0)
            self.assertEqual(CropInfo.objects.count(), 0)
            self.assertEqual(summary["results"]["totals"]["error"], 0)
            self.assertGreater(summary["results"]["totals"]["skipped"], 0)
            self.assertEqual(summary["results"]["totals"]["created"], 0)
            self.assertEqual(summary["results"]["totals"]["updated"], 0)

    def test_clean_fixture_apply_creates_rows_and_has_zero_errors(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-apply.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            self.assertEqual(summary["results"]["totals"]["error"], 0)
            self.assertGreater(summary["results"]["totals"]["created"], 0)
            self.assertEqual(summary["results"]["totals"]["updated"], 0)
            self.assertEqual(Block.objects.count(), 1)
            self.assertEqual(CropInfo.objects.count(), 1)
            self.assertEqual(CropBySeason.objects.count(), 1)
            self.assertEqual(SalesChannel.objects.count(), 1)
            self.assertEqual(CropSalesFormat.objects.count(), 1)

    def test_summary_artifact_defaults_to_import_artifacts_directory_when_unspecified(self):
        with TemporaryDirectory() as data_dir:
            self._write_clean_fixture(data_dir)
            call_command("import_historical_data", data_dir, "--validate-only")

            artifact_dir = Path(data_dir) / "_import_artifacts"
            artifacts = list(artifact_dir.glob("historical-import-summary-*.json"))
            self.assertEqual(len(artifacts), 1)
            summary = json.loads(artifacts[0].read_text(encoding="utf-8"))
            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)

    def test_summary_artifact_honors_explicit_output_path(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            summary_path = Path(output_dir) / "explicit-summary.json"

            call_command("import_historical_data", data_dir, "--validate-only", "--summary-json", str(summary_path))

            self.assertTrue(summary_path.exists())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)

    def test_apply_with_year_fixture_creates_planting_without_crop_season_error(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            self._write_year_fixture(data_dir, year=2021)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-year-apply.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["results"]["models"]["Planting"]["error"], 0)
            self.assertEqual(summary["results"]["models"]["Planting"]["created"], 1)
            self.assertEqual(PlanningYear.objects.count(), 1)
            self.assertEqual(Planting.objects.count(), 1)

    def test_field_walk_notes_can_resolve_planting_without_planting_id(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            self._write_year_fixture(data_dir, year=2021)
            year_dir = Path(data_dir) / "year_2021"
            self._write_csv(
                year_dir,
                "field_walk_notes.csv",
                [
                    "Planting ID,Walk Date,Condition,Yield Adjust %,Notes,Crop // Variety,Block,Bed,Plan Field Year,Plan Field Week",
                    ",2021-04-10,Good,95,resolved-by-lookup,Carrot // Nantes,Field 1,1,2021,13",
                ],
            )
            summary = self._run_import(data_dir, Path(output_dir) / "summary-field-walk-lookup.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            self.assertEqual(summary["results"]["models"]["FieldWalkNote"]["created"], 1)
            self.assertEqual(summary["results"]["models"]["FieldWalkNote"]["skipped"], 0)
            self.assertEqual(summary["results"]["models"]["FieldWalkNote"]["error"], 0)
            self.assertEqual(FieldWalkNote.objects.count(), 1)
            note = FieldWalkNote.objects.get()
            self.assertEqual(note.walk_date.isoformat(), "2021-04-10")
            self.assertEqual(note.yield_adjust_pct, 95)
            self.assertEqual(note.notes, "resolved-by-lookup")
            self.assertEqual(note.planting.variety, "Nantes")

    def test_known_mismatch_fixture_validate_only_reports_expected_skips_and_errors(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-mismatch-preflight.json", "--validate-only")

            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
            crop_by_season = summary["results"]["models"]["CropBySeason"]
            self.assertEqual(crop_by_season["error"], 2)
            self.assertEqual(crop_by_season["skipped"], 1)
            self.assertEqual(crop_by_season["created"], 0)
            self.assertEqual(crop_by_season["updated"], 0)
            self.assertEqual(CropBySeason.objects.count(), 0)
            self.assertEqual(Block.objects.count(), 0)
            self.assertEqual(CropInfo.objects.count(), 0)
            self.assertEqual(SalesChannel.objects.count(), 0)
            self.assertEqual(CropSalesFormat.objects.count(), 0)

    def test_known_mismatch_fixture_apply_reports_expected_skips_and_errors(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-mismatch-apply.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            crop_by_season = summary["results"]["models"]["CropBySeason"]
            self.assertEqual(crop_by_season["error"], 2)
            self.assertEqual(crop_by_season["skipped"], 1)
            self.assertEqual(crop_by_season["created"], 0)
            self.assertEqual(crop_by_season["updated"], 0)
            self.assertEqual(CropBySeason.objects.count(), 0)
            # Apply mode should still persist valid non-dependent reference rows.
            self.assertEqual(Block.objects.count(), 1)
            self.assertEqual(CropInfo.objects.count(), 1)
            self.assertEqual(SalesChannel.objects.count(), 1)
            self.assertEqual(CropSalesFormat.objects.count(), 1)

    def test_known_mismatch_fixture_reports_structured_row_errors(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-mismatch-row-errors.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            row_errors = summary["results"]["row_errors"]
            self._assert_deterministic_row_errors(
                row_errors,
                [
                    (
                        "CropBySeason",
                        1,
                        "namespace_mismatch",
                        "crop_by_season.block_type",
                        "unsupported block type 'Unknown Block'",
                    ),
                    (
                        "CropBySeason",
                        2,
                        "stale_fk",
                        "crop_by_season.crop",
                        "crop not found 'Ghost Crop'",
                    ),
                    (
                        "CropSalesFormat",
                        2,
                        "stale_fk",
                        "crop_sales_formats.crop",
                        "crop not found 'Ghost Crop'",
                    ),
                ],
            )

    def test_known_mismatch_fixture_emits_failure_signature_ownership_mapping(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-mismatch-signatures.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            signatures = {item["signature"]: item for item in summary["results"]["failure_signatures"]}
            self.assertEqual(set(signatures.keys()), {"namespace_mismatch", "stale_fk"})
            self.assertEqual(signatures["namespace_mismatch"]["count"], 1)
            self.assertEqual(signatures["namespace_mismatch"]["owner_area"], "data-contracts")
            self.assertEqual(signatures["namespace_mismatch"]["owner_team"], "import-pipeline")
            self.assertEqual(signatures["namespace_mismatch"]["severity"], "medium")
            self.assertEqual(
                signatures["namespace_mismatch"]["escalation_path"],
                "ops-oncall -> data-contracts",
            )
            self.assertEqual(
                signatures["namespace_mismatch"]["example"]["field_path"], "crop_by_season.block_type"
            )
            self.assertEqual(signatures["stale_fk"]["count"], 2)
            self.assertEqual(signatures["stale_fk"]["owner_area"], "reference-data")
            self.assertEqual(signatures["stale_fk"]["owner_team"], "import-pipeline")
            self.assertEqual(signatures["stale_fk"]["severity"], "high")
            self.assertEqual(signatures["stale_fk"]["escalation_path"], "ops-oncall -> reference-data")

    def test_known_mismatch_fixture_emits_grouped_escalation_summary(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-mismatch-escalation-summary.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            escalation_summary = summary["results"]["escalation_summary"]
            self._assert_escalation_summary_payload_contract(escalation_summary)
            self.assertEqual(
                escalation_summary,
                [
                    {
                        "owner_area": "reference-data",
                        "owner_team": "import-pipeline",
                        "severity": "high",
                        "escalation_path": "ops-oncall -> reference-data",
                        "count": 2,
                        "signatures": ["stale_fk"],
                        "recovery_steps": [
                            "seed missing reference rows and rerun --validate-only",
                        ],
                    },
                    {
                        "owner_area": "data-contracts",
                        "owner_team": "import-pipeline",
                        "severity": "medium",
                        "escalation_path": "ops-oncall -> data-contracts",
                        "count": 1,
                        "signatures": ["namespace_mismatch"],
                        "recovery_steps": [
                            "correct source value namespaces and rerun --validate-only",
                        ],
                    },
                ],
            )

    def test_failure_signature_unknown_code_uses_fallback_ownership_mapping(self):
        from core.management.commands.import_historical_data import Command

        command = Command()
        command.row_errors = [
            {
                "model": "Synthetic",
                "row": 1,
                "code": "unclassified_error",
                "field_path": "synthetic.field",
                "message": "synthetic error",
            }
        ]
        signatures = command._build_failure_signatures(status="ok", fatal_error=None)
        self.assertEqual(len(signatures), 1)
        signature = signatures[0]
        self.assertEqual(signature["signature"], "unclassified_error")
        self.assertEqual(signature["owner_area"], "triage")
        self.assertEqual(signature["owner_team"], "platform")
        self.assertEqual(signature["severity"], "high")
        self.assertEqual(signature["escalation_path"], "ops-oncall -> platform")

    def test_failed_status_appends_fatal_import_exception_signature_with_platform_owner(self):
        from core.management.commands.import_historical_data import Command

        command = Command()
        command.row_errors = []
        signatures = command._build_failure_signatures(status="failed", fatal_error="boom")
        self.assertEqual(len(signatures), 1)
        signature = signatures[0]
        self.assertEqual(signature["signature"], "fatal_import_exception")
        self.assertEqual(signature["count"], 1)
        self.assertEqual(signature["owner_area"], "import-runtime")
        self.assertEqual(signature["owner_team"], "platform")
        self.assertEqual(signature["severity"], "high")
        self.assertEqual(signature["escalation_path"], "ops-oncall -> platform")
        self.assertEqual(signature["example"]["model"], "ImportRun")
        self.assertEqual(signature["example"]["field_path"], "run")

    def test_repo_mismatch_fixture_matrix_has_stale_fk_and_namespace_mismatch_signals(self):
        fixture_dir = Path(__file__).resolve().parents[2] / "data" / "import_fixtures" / "mismatch"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture.json",
                "--validate-only",
            )

            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
            self.assertEqual(summary["results"]["models"]["CropBySeason"]["error"], 6)
            self.assertEqual(summary["results"]["models"]["CropBySeason"]["skipped"], 1)
            self.assertEqual(summary["results"]["models"]["CropSalesFormat"]["error"], 3)
            self.assertEqual(summary["results"]["models"]["Planting"]["error"], 6)
            row_errors = summary["results"]["row_errors"]
            self._assert_deterministic_row_errors(
                row_errors,
                [
                    (
                        "CropBySeason",
                        1,
                        "namespace_mismatch",
                        "crop_by_season.block_type",
                        "unsupported block type 'Unknown Block'",
                    ),
                    (
                        "CropBySeason",
                        2,
                        "namespace_mismatch",
                        "crop_by_season.block_type",
                        "unsupported block type 'Unknown Tunnel'",
                    ),
                    (
                        "CropBySeason",
                        3,
                        "namespace_mismatch",
                        "crop_by_season.block_type",
                        "unsupported block type 'Storage Barn'",
                    ),
                    (
                        "CropBySeason",
                        4,
                        "namespace_mismatch",
                        "crop_by_season.block_type",
                        "unsupported block type 'Container Yard'",
                    ),
                    (
                        "CropBySeason",
                        5,
                        "stale_fk",
                        "crop_by_season.crop",
                        "crop not found 'Ghost Crop'",
                    ),
                    (
                        "CropBySeason",
                        6,
                        "stale_fk",
                        "crop_by_season.crop",
                        "crop not found 'Missing Crop'",
                    ),
                    (
                        "CropSalesFormat",
                        2,
                        "stale_fk",
                        "crop_sales_formats.crop",
                        "crop not found 'Ghost Crop'",
                    ),
                    (
                        "CropSalesFormat",
                        3,
                        "stale_fk",
                        "crop_sales_formats.crop",
                        "crop not found 'Missing Crop'",
                    ),
                    (
                        "CropSalesFormat",
                        4,
                        "stale_fk",
                        "crop_sales_formats.crop",
                        "crop not found 'Phantom Crop'",
                    ),
                    (
                        "Planting",
                        1,
                        "stale_fk",
                        "plantings.crop",
                        "crop not found 'Missing Crop'",
                    ),
                    (
                        "Planting",
                        2,
                        "stale_fk",
                        "plantings.block",
                        "block not found 'Missing Block'",
                    ),
                    (
                        "Planting",
                        3,
                        "stale_fk",
                        "plantings.block",
                        "block not found 'Ghost Block'",
                    ),
                    (
                        "Planting",
                        4,
                        "stale_fk",
                        "plantings.block",
                        "block not found 'Shadow Block'",
                    ),
                    (
                        "Planting",
                        5,
                        "stale_fk",
                        "plantings.crop",
                        "crop not found 'Phantom Crop'",
                    ),
                    (
                        "Planting",
                        1,
                        "stale_fk",
                        "plantings.planning_year",
                        "planning year not found '2022'",
                    ),
                ],
            )
            # Deterministic ordering from importer pass order helps gate snapshots stay stable.
            self.assertEqual([item["row"] for item in row_errors], [1, 2, 3, 4, 5, 6, 2, 3, 4, 1, 2, 3, 4, 5, 1])

    def test_repo_mismatch_fixture_matrix_validate_and_apply_have_identical_row_error_payloads(self):
        fixture_dir = Path(__file__).resolve().parents[2] / "data" / "import_fixtures" / "mismatch"
        with TemporaryDirectory() as output_dir:
            validate_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-validate-only-payload.json",
                "--validate-only",
            )
            apply_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-apply-payload.json",
            )

        validate_errors = validate_summary["results"]["row_errors"]
        apply_errors = apply_summary["results"]["row_errors"]
        self._assert_row_error_payload_contract(validate_errors)
        self._assert_row_error_payload_contract(apply_errors)
        self.assertEqual(validate_errors, apply_errors)

    def test_repo_mismatch_fixture_manifest_row_errors_follow_payload_contract(self):
        fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"
        fixture_dir = fixture_root / "mismatch"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        self._assert_row_error_payload_contract(manifest["expected"]["validate_only"]["row_errors"])
        self._assert_row_error_payload_contract(manifest["expected"]["apply"]["row_errors"])

    def test_repo_mismatch_fixture_validate_only_emits_expected_error_signals(self):
        fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"
        fixture_dir = fixture_root / "mismatch"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        with TemporaryDirectory() as output_dir:
            summary, _stdout, stderr = self._run_import_with_output(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-validate-signals.json",
                "--validate-only",
            )

        self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
        for message in manifest["expected"]["validate_only"]["error_signals"]:
            with self.subTest(signal=message):
                self.assertIn(message, stderr)

    def test_repo_mismatch_fixture_apply_emits_expected_error_signals(self):
        fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"
        fixture_dir = fixture_root / "mismatch"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        with TemporaryDirectory() as output_dir:
            summary, _stdout, stderr = self._run_import_with_output(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-apply-signals.json",
            )

        self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
        for message in manifest["expected"]["apply"]["error_signals"]:
            with self.subTest(signal=message):
                self.assertIn(message, stderr)

    def test_repo_mismatch_fixture_validate_only_prints_escalation_handoff_buckets(self):
        fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"
        fixture_dir = fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            summary, stdout, _stderr = self._run_import_with_output(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-validate-escalation-handoff.json",
                "--validate-only",
            )

        self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
        self.assertIn("🚨 ESCALATION HANDOFF", stdout)
        self.assertIn(
            "high | reference-data | import-pipeline | ops-oncall -> reference-data | count=11 | signatures=stale_fk",
            stdout,
        )
        self.assertIn(
            "recovery=seed missing reference rows and rerun --validate-only",
            stdout,
        )
        self.assertIn(
            "medium | data-contracts | import-pipeline | ops-oncall -> data-contracts | count=4 | signatures=namespace_mismatch",
            stdout,
        )
        self.assertIn(
            "recovery=correct source value namespaces and rerun --validate-only",
            stdout,
        )

    def test_repo_mismatch_fixture_matrix_row_error_classes_and_counts_are_deterministic(self):
        fixture_dir = Path(__file__).resolve().parents[2] / "data" / "import_fixtures" / "mismatch"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-error-classes.json",
                "--validate-only",
            )

        row_errors = summary["results"]["row_errors"]
        self._assert_row_error_payload_contract(row_errors)
        classes = [item["code"] for item in row_errors]
        self.assertEqual(
            classes,
            [
                "namespace_mismatch",
                "namespace_mismatch",
                "namespace_mismatch",
                "namespace_mismatch",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
                "stale_fk",
            ],
        )
        self.assertEqual(classes.count("namespace_mismatch"), 4)
        self.assertEqual(classes.count("stale_fk"), 11)

    def test_repo_mismatch_fixture_matrix_row_error_model_distribution_is_deterministic(self):
        fixture_dir = Path(__file__).resolve().parents[2] / "data" / "import_fixtures" / "mismatch"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-model-distribution.json",
                "--validate-only",
            )

        row_errors = summary["results"]["row_errors"]
        self._assert_row_error_payload_contract(row_errors)
        model_counts = {}
        for item in row_errors:
            model_counts[item["model"]] = model_counts.get(item["model"], 0) + 1
        self.assertEqual(
            model_counts,
            {
                "CropBySeason": 6,
                "CropSalesFormat": 3,
                "Planting": 6,
            },
        )

    def test_repo_clean_fixture_pack_validate_only_matches_manifest_expectations(self):
        fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"
        fixture_dir = fixture_root / "clean"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-clean-fixture-preflight.json",
                "--validate-only",
            )

        self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
        expected = manifest["expected"]["validate_only"]
        self.assertEqual(summary["status"], expected["status"])
        self.assertEqual(summary["results"]["totals"], expected["totals"])
        self.assertEqual(summary["results"]["models"]["Block"], expected["models"]["Block"])

    def test_repo_sample_import_apply_reconciles_summary_counts_to_persisted_models(self):
        fixture_dir = Path(__file__).resolve().parents[2] / "data" / "sample_import"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-sample-fixture-apply-reconciliation.json",
            )

        self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["results"]["row_errors"], [])
        self.assertEqual(summary["results"]["failure_signatures"], [])
        self.assertEqual(summary["results"]["escalation_summary"], [])
        self.assertEqual(summary["results"]["totals"]["error"], 0)
        self.assertEqual(summary["results"]["totals"]["skipped"], 0)

        expected_counts = {
            "Block": Block.objects.count(),
            "CropInfo": CropInfo.objects.count(),
            "CropBySeason": CropBySeason.objects.count(),
            "SalesChannel": SalesChannel.objects.count(),
            "CropSalesFormat": CropSalesFormat.objects.count(),
            "PlanningYear": PlanningYear.objects.count(),
            "Planting": Planting.objects.count(),
            "NurseryEvent": NurseryEvent.objects.count(),
            "HarvestEvent": HarvestEvent.objects.count(),
            "FieldWalkNote": FieldWalkNote.objects.count(),
            "InventoryLedger": InventoryLedger.objects.count(),
            "SalesEvent": SalesEvent.objects.count(),
            "QuickSalesEntry": QuickSalesEntry.objects.count(),
            "RotationHistory": RotationHistory.objects.count(),
        }
        for model_name, persisted_count in expected_counts.items():
            with self.subTest(model=model_name):
                self.assertEqual(summary["results"]["models"][model_name]["created"], persisted_count)
                self.assertEqual(summary["results"]["models"][model_name]["updated"], 0)
                self.assertEqual(summary["results"]["models"][model_name]["skipped"], 0)
                self.assertEqual(summary["results"]["models"][model_name]["error"], 0)

    def test_repo_sample_import_apply_supports_curated_post_import_sanity_queries(self):
        fixture_dir = Path(__file__).resolve().parents[2] / "data" / "sample_import"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-sample-fixture-apply-sanity-queries.json",
            )

        self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["results"]["totals"]["error"], 0)
        self.assertEqual(summary["results"]["row_errors"], [])

        self.assertEqual(PlanningYear.objects.filter(year=2023).count(), 1)
        self.assertEqual(PlanningYear.objects.filter(year=2024).count(), 1)
        self.assertTrue(
            Planting.objects.filter(
                planning_year__year=2023,
                crop__name="Tomato Beefsteak",
                block__name="North Field",
            ).exists()
        )
        self.assertTrue(
            InventoryLedger.objects.filter(
                crop__name="Tomato Beefsteak",
            ).exists()
        )
        self.assertTrue(
            SalesEvent.objects.filter(
                channel__name="Farmers Market Saturday",
            ).exists()
        )
        self.assertTrue(
            QuickSalesEntry.objects.filter(
                channel__name="Farm Store",
                total_cash__gt=0,
            ).exists()
        )

    def test_repo_mismatch_fixture_pack_apply_matches_manifest_expectations(self):
        fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"
        fixture_dir = fixture_root / "mismatch"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-apply.json",
            )

        self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
        expected = manifest["expected"]["apply"]
        self.assertEqual(summary["status"], expected["status"])
        self.assertEqual(summary["results"]["totals"], expected["totals"])
        self.assertEqual(summary["results"]["models"]["Block"], expected["models"]["Block"])
        self.assertEqual(summary["results"]["models"]["CropBySeason"], expected["models"]["CropBySeason"])
        self.assertEqual(
            summary["results"]["models"]["CropSalesFormat"],
            expected["models"]["CropSalesFormat"],
        )
        self._assert_deterministic_row_errors(
            summary["results"]["row_errors"],
            [
                (
                    item["model"],
                    item["row"],
                    item["code"],
                    item["field_path"],
                    item["message"],
                )
                for item in expected["row_errors"]
            ],
        )

    def test_repo_mismatch_fixture_pack_validate_only_matches_manifest_expectations(self):
        fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"
        fixture_dir = fixture_root / "mismatch"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-repo-mismatch-fixture-validate-only.json",
                "--validate-only",
            )

        self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
        expected = manifest["expected"]["validate_only"]
        self.assertEqual(summary["status"], expected["status"])
        self.assertEqual(summary["results"]["totals"], expected["totals"])
        self.assertEqual(summary["results"]["models"]["Block"], expected["models"]["Block"])
        self.assertEqual(summary["results"]["models"]["CropBySeason"], expected["models"]["CropBySeason"])
        self.assertEqual(
            summary["results"]["models"]["CropSalesFormat"],
            expected["models"]["CropSalesFormat"],
        )
        self._assert_deterministic_row_errors(
            summary["results"]["row_errors"],
            [
                (
                    item["model"],
                    item["row"],
                    item["code"],
                    item["field_path"],
                    item["message"],
                )
                for item in expected["row_errors"]
            ],
        )

    def test_known_mismatch_fixture_has_deterministic_outcomes_across_apply_and_preflight(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            preflight_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-mismatch-preflight-deterministic.json",
                "--preflight",
            )
            apply_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-mismatch-apply-deterministic.json",
            )

            self.assertEqual(preflight_summary["status"], "ok")
            self.assertEqual(apply_summary["status"], "ok")
            self.assertEqual(preflight_summary["fatal_error"], None)
            self.assertEqual(apply_summary["fatal_error"], None)
            self.assertEqual(preflight_summary["run"]["validate_only"], True)
            self.assertEqual(apply_summary["run"]["validate_only"], False)
            self.assertEqual(
                preflight_summary["results"]["models"]["CropBySeason"],
                {"created": 0, "updated": 0, "skipped": 1, "error": 2},
            )
            self.assertEqual(
                apply_summary["results"]["models"]["CropBySeason"],
                {"created": 0, "updated": 0, "skipped": 1, "error": 2},
            )
            self.assertEqual(
                preflight_summary["results"]["models"]["CropSalesFormat"],
                {"created": 0, "updated": 0, "skipped": 1, "error": 1},
            )
            self.assertEqual(
                apply_summary["results"]["models"]["CropSalesFormat"],
                {"created": 1, "updated": 0, "skipped": 0, "error": 1},
            )
            self.assertEqual(preflight_summary["results"]["totals"]["error"], 3)
            self.assertEqual(apply_summary["results"]["totals"]["error"], 3)
            self.assertEqual(CropBySeason.objects.count(), 0)

    def test_edge_case_fixture_preflight_enforces_expected_error_and_skip_matrix(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_edge_case_fixture(data_dir)
            summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-edge-case-preflight.json",
                "--preflight",
            )

            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["results"]["models"]["CropBySeason"]["error"], 1)
            self.assertEqual(summary["results"]["models"]["CropBySeason"]["skipped"], 1)
            self.assertEqual(summary["results"]["models"]["CropSalesFormat"]["error"], 1)
            self._assert_deterministic_row_errors(
                summary["results"]["row_errors"],
                [
                    (
                        "CropBySeason",
                        2,
                        "namespace_mismatch",
                        "crop_by_season.block_type",
                        "unsupported block type 'Unknown Block'",
                    ),
                    (
                        "CropSalesFormat",
                        2,
                        "stale_fk",
                        "crop_sales_formats.crop",
                        "crop not found 'Ghost Crop'",
                    ),
                ],
            )

    def test_edge_case_fixture_apply_persists_valid_rows_and_preserves_error_payloads(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_edge_case_fixture(data_dir)
            preflight_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-edge-case-preflight-compare.json",
                "--validate-only",
            )
            apply_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-edge-case-apply.json",
            )

            self._assert_summary_contract(apply_summary, expected_validate_only=False, expected_dry_run=False)
            self.assertEqual(apply_summary["results"]["models"]["CropBySeason"]["error"], 1)
            self.assertEqual(apply_summary["results"]["models"]["CropBySeason"]["skipped"], 1)
            self.assertEqual(apply_summary["results"]["models"]["CropSalesFormat"]["error"], 1)
            self.assertEqual(Block.objects.count(), 1)
            self.assertEqual(CropInfo.objects.count(), 1)
            self.assertEqual(SalesChannel.objects.count(), 1)
            self.assertEqual(CropSalesFormat.objects.count(), 1)
            self.assertEqual(CropBySeason.objects.count(), 0)
            self.assertEqual(
                preflight_summary["results"]["row_errors"],
                apply_summary["results"]["row_errors"],
            )

    def test_operations_and_sales_invalid_rows_emit_structured_errors_instead_of_silent_skips(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            self._write_ops_sales_error_fixture(data_dir, year=2021)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-ops-sales-row-errors.json")

        self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
        observed = {
            (item["model"], item["code"], item["field_path"], item["message"])
            for item in summary["results"]["row_errors"]
        }
        expected_subset = {
            (
                "InventoryLedger",
                "missing_required",
                "inventory_ledger.crop",
                "missing required value for 'Crop Name'",
            ),
            (
                "InventoryLedger",
                "stale_fk",
                "inventory_ledger.crop",
                "crop not found 'Ghost Crop'",
            ),
            (
                "InventoryLedger",
                "missing_required",
                "inventory_ledger.event_date",
                "missing required value for 'Event Date'",
            ),
            (
                "PackAllocation",
                "missing_required",
                "pack_allocations.product",
                "missing required value for 'Product'",
            ),
            (
                "PackAllocation",
                "stale_fk",
                "pack_allocations.channel",
                "sales channel not found 'Ghost Channel'",
            ),
            (
                "PackAllocation",
                "stale_fk",
                "pack_allocations.product",
                "product not found 'Ghost Product'",
            ),
            (
                "SalesEvent",
                "missing_required",
                "sales_events.sale_date",
                "missing required value for 'Sale Date'",
            ),
            (
                "SalesEvent",
                "stale_fk",
                "sales_events.channel",
                "sales channel not found 'Ghost Channel'",
            ),
            (
                "SalesEvent",
                "stale_fk",
                "sales_events.product",
                "product not found 'Ghost Product'",
            ),
            (
                "QuickSalesEntry",
                "missing_required",
                "quick_sales_entries.channel",
                "missing required value for 'Channel Name'",
            ),
            (
                "QuickSalesEntry",
                "stale_fk",
                "quick_sales_entries.channel",
                "sales channel not found 'Ghost Channel'",
            ),
        }
        self.assertTrue(expected_subset <= observed)

        signature_counts = {
            item["signature"]: item["count"] for item in summary["results"]["failure_signatures"]
        }
        self.assertGreaterEqual(signature_counts.get("missing_required", 0), 4)
        self.assertGreaterEqual(signature_counts.get("stale_fk", 0), 6)

    def test_mixed_batch_fixture_validate_only_and_apply_keep_partial_failure_evidence_identical(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_mixed_batch_fixture(data_dir, year=2021)
            validate_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-mixed-batch-validate.json",
                "--validate-only",
            )
            apply_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-mixed-batch-apply.json",
            )

        self._assert_summary_contract(validate_summary, expected_validate_only=True, expected_dry_run=False)
        self._assert_summary_contract(apply_summary, expected_validate_only=False, expected_dry_run=False)
        self.assertEqual(validate_summary["results"]["row_errors"], apply_summary["results"]["row_errors"])
        self.assertEqual(
            validate_summary["results"]["failure_signatures"],
            apply_summary["results"]["failure_signatures"],
        )
        self.assertEqual(
            validate_summary["results"]["escalation_summary"],
            apply_summary["results"]["escalation_summary"],
        )
        self.assertEqual(validate_summary["results"]["totals"]["error"], 0)
        self.assertEqual(validate_summary["results"]["totals"]["skipped"], 23)
        self.assertEqual(apply_summary["results"]["totals"]["error"], 0)
        self.assertEqual(apply_summary["results"]["totals"]["skipped"], 11)
        self.assertEqual(apply_summary["results"]["models"]["InventoryLedger"]["created"], 1)
        self.assertEqual(apply_summary["results"]["models"]["InventoryLedger"]["skipped"], 3)
        self.assertEqual(apply_summary["results"]["models"]["InventoryLedger"]["error"], 0)
        self.assertEqual(apply_summary["results"]["models"]["PackAllocation"]["created"], 1)
        self.assertEqual(apply_summary["results"]["models"]["PackAllocation"]["skipped"], 3)
        self.assertEqual(apply_summary["results"]["models"]["PackAllocation"]["error"], 0)
        self.assertEqual(apply_summary["results"]["models"]["SalesEvent"]["created"], 1)
        self.assertEqual(apply_summary["results"]["models"]["SalesEvent"]["skipped"], 3)
        self.assertEqual(apply_summary["results"]["models"]["SalesEvent"]["error"], 0)
        self.assertEqual(apply_summary["results"]["models"]["QuickSalesEntry"]["created"], 1)
        self.assertEqual(apply_summary["results"]["models"]["QuickSalesEntry"]["skipped"], 2)
        self.assertEqual(apply_summary["results"]["models"]["QuickSalesEntry"]["error"], 0)
        self.assertEqual(InventoryLedger.objects.count(), 1)
        self.assertEqual(SalesEvent.objects.count(), 1)
        self.assertEqual(QuickSalesEntry.objects.count(), 1)
        self.assertEqual(
            [(item["signature"], item["count"]) for item in apply_summary["results"]["failure_signatures"]],
            [("missing_required", 5), ("stale_fk", 6)],
        )
        self.assertEqual(
            apply_summary["results"]["escalation_summary"],
            [
                {
                    "owner_area": "reference-data",
                    "owner_team": "import-pipeline",
                    "severity": "high",
                    "escalation_path": "ops-oncall -> reference-data",
                    "count": 6,
                    "signatures": ["stale_fk"],
                    "recovery_steps": [
                        "seed missing reference rows and rerun --validate-only",
                    ],
                },
                {
                    "owner_area": "data-contracts",
                    "owner_team": "import-pipeline",
                    "severity": "medium",
                    "escalation_path": "ops-oncall -> data-contracts",
                    "count": 5,
                    "signatures": ["missing_required"],
                    "recovery_steps": [
                        "populate required source fields and rerun --validate-only",
                    ],
                },
            ],
        )

    def test_mixed_batch_fixture_apply_replays_keep_partial_failure_totals_stable(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_mixed_batch_fixture(data_dir, year=2021)
            first_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-mixed-batch-first.json",
            )
            second_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-mixed-batch-second.json",
            )
            third_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-mixed-batch-third.json",
            )

        self.assertGreater(first_summary["results"]["totals"]["created"], 0)
        self.assertEqual(second_summary["results"]["totals"], third_summary["results"]["totals"])
        self.assertEqual(second_summary["results"]["row_errors"], third_summary["results"]["row_errors"])
        self.assertEqual(
            second_summary["results"]["failure_signatures"],
            third_summary["results"]["failure_signatures"],
        )
        self.assertEqual(
            second_summary["results"]["escalation_summary"],
            third_summary["results"]["escalation_summary"],
        )
        self.assertEqual(second_summary["results"]["totals"]["error"], 0)
        self.assertEqual(second_summary["results"]["totals"]["skipped"], 11)
        self.assertEqual(second_summary["results"]["models"]["InventoryLedger"]["skipped"], 3)
        self.assertEqual(second_summary["results"]["models"]["InventoryLedger"]["error"], 0)
        self.assertEqual(second_summary["results"]["models"]["PackAllocation"]["skipped"], 3)
        self.assertEqual(second_summary["results"]["models"]["PackAllocation"]["error"], 0)
        self.assertEqual(second_summary["results"]["models"]["SalesEvent"]["skipped"], 3)
        self.assertEqual(second_summary["results"]["models"]["SalesEvent"]["error"], 0)
        self.assertEqual(second_summary["results"]["models"]["QuickSalesEntry"]["skipped"], 2)
        self.assertEqual(second_summary["results"]["models"]["QuickSalesEntry"]["error"], 0)
        self.assertEqual(InventoryLedger.objects.count(), 1)
        self.assertEqual(SalesEvent.objects.count(), 1)
        self.assertEqual(QuickSalesEntry.objects.count(), 1)

    def test_crop_by_season_block_type_normalization_accepts_case_and_spacing_variants(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_block_type_normalization_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-block-type-normalized.json")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
            self.assertEqual(summary["results"]["models"]["CropBySeason"]["error"], 0)
            self.assertEqual(summary["results"]["models"]["CropBySeason"]["created"], 1)
            self.assertEqual(CropBySeason.objects.count(), 1)
            self.assertEqual(CropBySeason.objects.first().block_type, "high_tunnel")

    def test_clean_fixture_apply_is_idempotent_on_repeat_runs(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            first_summary = self._run_import(data_dir, Path(output_dir) / "summary-first.json")
            second_summary = self._run_import(data_dir, Path(output_dir) / "summary-second.json")

            self.assertGreater(first_summary["results"]["totals"]["created"], 0)
            self.assertEqual(first_summary["results"]["totals"]["updated"], 0)
            self.assertEqual(first_summary["results"]["totals"]["error"], 0)
            self.assertEqual(second_summary["results"]["totals"]["created"], 0)
            self.assertGreater(second_summary["results"]["totals"]["updated"], 0)
            self.assertEqual(second_summary["results"]["totals"]["error"], 0)

    def test_clean_fixture_validate_only_is_idempotent_on_repeat_runs(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            first_summary = self._run_import(data_dir, Path(output_dir) / "summary-preflight-first.json", "--validate-only")
            second_summary = self._run_import(data_dir, Path(output_dir) / "summary-preflight-second.json", "--validate-only")

            self._assert_summary_contract(first_summary, expected_validate_only=True, expected_dry_run=False)
            self._assert_summary_contract(second_summary, expected_validate_only=True, expected_dry_run=False)
            self.assertEqual(Block.objects.count(), 0)
            self.assertEqual(CropInfo.objects.count(), 0)
            self.assertEqual(first_summary["results"]["totals"], second_summary["results"]["totals"])

    def test_preflight_alias_matches_validate_only_behavior(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            validate_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-validate-only.json",
                "--validate-only",
            )
            preflight_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-preflight-alias.json",
                "--preflight",
            )

            self.assertEqual(validate_summary["run"]["validate_only"], True)
            self.assertEqual(preflight_summary["run"]["validate_only"], True)
            self.assertEqual(validate_summary["results"]["totals"], preflight_summary["results"]["totals"])
            self.assertEqual(
                validate_summary["results"]["models"]["CropBySeason"],
                preflight_summary["results"]["models"]["CropBySeason"],
            )

    def test_clean_fixture_dry_run_sets_mode_flags_and_keeps_database_unchanged(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-dry-run.json", "--dry-run")

            self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=True)
            self.assertEqual(summary["results"]["totals"]["created"], 0)
            self.assertEqual(summary["results"]["totals"]["updated"], 0)
            self.assertEqual(summary["results"]["totals"]["error"], 0)
            self.assertGreater(summary["results"]["totals"]["skipped"], 0)
            self.assertEqual(Block.objects.count(), 0)
            self.assertEqual(CropInfo.objects.count(), 0)
            self.assertEqual(CropBySeason.objects.count(), 0)

    def test_summary_run_id_uses_microsecond_precision_for_retry_safety(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            first_summary = self._run_import(data_dir, Path(output_dir) / "summary-first-run-id.json", "--validate-only")
            second_summary = self._run_import(data_dir, Path(output_dir) / "summary-second-run-id.json", "--validate-only")

        self.assertEqual(len(first_summary["run"]["run_id"]), 21)
        self.assertEqual(len(second_summary["run"]["run_id"]), 21)
        self.assertNotEqual(first_summary["run"]["run_id"], second_summary["run"]["run_id"])

    def test_validate_only_takes_precedence_when_dry_run_flag_is_also_passed(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-validate-precedence.json",
                "--validate-only",
                "--dry-run",
            )

            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
            crop_by_season = summary["results"]["models"]["CropBySeason"]
            self.assertEqual(crop_by_season["error"], 2)
            self.assertEqual(crop_by_season["skipped"], 1)
            self.assertEqual(CropBySeason.objects.count(), 0)
            self.assertEqual(Block.objects.count(), 0)
            self.assertTrue(summary["run"]["atomic_apply"])

    def test_validate_only_ignores_non_atomic_apply_override_and_remains_transactional(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_known_mismatch_fixture(data_dir)
            summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-validate-only-ignores-non-atomic.json",
                "--validate-only",
                "--non-atomic-apply",
            )

            self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
            self.assertTrue(summary["run"]["atomic_apply"])
            self.assertEqual(Block.objects.count(), 0)

    def test_apply_mode_rolls_back_all_writes_when_pipeline_raises_with_atomic_apply_enabled(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            summary_path = Path(output_dir) / "summary-failed-atomic.json"
            with patch(
                "core.management.commands.import_historical_data.Command._import_years_and_plantings",
                side_effect=RuntimeError("simulated pipeline failure"),
            ):
                with self.assertRaises(SystemExit):
                    call_command(
                        "import_historical_data",
                        data_dir,
                        "--summary-json",
                        str(summary_path),
                    )

            self.assertEqual(Block.objects.count(), 0)
            self.assertEqual(CropInfo.objects.count(), 0)
            self.assertEqual(CropBySeason.objects.count(), 0)
            failed_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(failed_summary["status"], "failed")
            self.assertTrue(failed_summary["run"]["atomic_apply"])
            self.assertIn(
                "RuntimeError: simulated pipeline failure [mode=apply, atomic_apply=True, dry_run=False]",
                failed_summary["fatal_error"],
            )
            self.assertEqual(
                failed_summary["results"]["failure_signatures"],
                [
                    {
                        "signature": "fatal_import_exception",
                        "count": 1,
                        "owner_area": "import-runtime",
                        "owner_team": "platform",
                        "severity": "high",
                        "escalation_path": "ops-oncall -> platform",
                        "recovery": "review fatal_error and importer logs before retry",
                        "example": {
                            "model": "ImportRun",
                            "field_path": "run",
                            "message": "RuntimeError: simulated pipeline failure [mode=apply, atomic_apply=True, dry_run=False]",
                        },
                    }
                ],
            )

    def test_apply_mode_can_disable_atomic_rollback_for_recovery_diagnostics(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            summary_path = Path(output_dir) / "summary-failed-non-atomic.json"
            with patch(
                "core.management.commands.import_historical_data.Command._import_years_and_plantings",
                side_effect=RuntimeError("simulated pipeline failure"),
            ):
                with self.assertRaises(SystemExit):
                    call_command(
                        "import_historical_data",
                        data_dir,
                        "--non-atomic-apply",
                        "--summary-json",
                        str(summary_path),
                    )

            self.assertEqual(Block.objects.count(), 1)
            self.assertEqual(CropInfo.objects.count(), 1)
            failed_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(failed_summary["status"], "failed")
            self.assertFalse(failed_summary["run"]["atomic_apply"])

    def test_historical_import_resolves_normalized_and_duplicate_channel_product_lookups(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            # Duplicate channel names are allowed; this verifies deterministic fallback.
            SalesChannel.objects.create(
                name="farm stand",
                days_of_week=["Saturday"],
                start_week=1,
                end_week=52,
                weekly_target="100.00",
                is_csa=False,
                allocation_priority=9,
            )
            carrot = CropInfo.objects.create(
                name="Backup Carrot",
                crop_type="Vegetables",
                botanical_family="Apiaceae",
                propagation_type="seed",
                is_perennial=False,
                fresh_or_storage="fresh",
                storage_weeks=0,
                harvest_unit="pounds",
                avg_unit_weight="1.00",
                nursery_weeks=0,
                weeks_until_pot_up=0,
                seeds_per_cell=1,
                thinned_plants=0,
            )
            CropSalesFormat.objects.create(
                crop=carrot,
                product_name="carrot bunch",
                sale_price="2.00",
                sale_unit="bunch",
                harvest_qty_per_sale_unit="1.00",
                is_active=True,
            )
            self._write_year_fixture(data_dir, year=2021)
            summary = self._run_import(data_dir, Path(output_dir) / "summary-normalized-lookup.json")

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["results"]["models"]["SalesEvent"]["error"], 0)
            self.assertEqual(summary["results"]["models"]["PackAllocation"]["error"], 0)
            self.assertEqual(summary["results"]["models"]["SalesEvent"]["created"], 1)
            self.assertEqual(summary["results"]["models"]["PackAllocation"]["created"], 1)

    def test_historical_import_emits_deterministic_warnings_for_duplicate_weak_id_matches(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_clean_fixture(data_dir)
            SalesChannel.objects.create(
                name="farm stand",
                days_of_week=["Saturday"],
                start_week=1,
                end_week=52,
                weekly_target="100.00",
                is_csa=False,
                allocation_priority=9,
            )
            carrot = CropInfo.objects.create(
                name="Backup Carrot",
                crop_type="Vegetables",
                botanical_family="Apiaceae",
                propagation_type="seed",
                is_perennial=False,
                fresh_or_storage="fresh",
                storage_weeks=0,
                harvest_unit="pounds",
                avg_unit_weight="1.00",
                nursery_weeks=0,
                weeks_until_pot_up=0,
                seeds_per_cell=1,
                thinned_plants=0,
            )
            CropSalesFormat.objects.create(
                crop=carrot,
                product_name="carrot bunch",
                sale_price="2.00",
                sale_unit="bunch",
                harvest_qty_per_sale_unit="1.00",
                is_active=True,
            )
            self._write_year_fixture(data_dir, year=2021)
            summary, stdout, _stderr = self._run_import_with_output(
                data_dir,
                Path(output_dir) / "summary-duplicate-weak-id-warning.json",
            )

        self.assertEqual(summary["status"], "ok")
        self.assertIn("Multiple normalized sales channel matches for 'FARM   STAND'; using id=", stdout)
        self.assertIn("Multiple normalized product matches for 'CARROT   BUNCH'; using id=", stdout)

    def test_partial_year_fixture_range_skips_missing_year_directories_without_fatal_errors(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            self._write_partial_year_fixture(data_dir, years=(2021, 2023))
            validate_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-partial-year-validate.json",
                "--validate-only",
                "--start-year",
                "2021",
                "--end-year",
                "2023",
            )
            apply_summary = self._run_import(
                data_dir,
                Path(output_dir) / "summary-partial-year-apply.json",
                "--start-year",
                "2021",
                "--end-year",
                "2023",
            )

        self._assert_summary_contract(validate_summary, expected_validate_only=True, expected_dry_run=False)
        self._assert_summary_contract(apply_summary, expected_validate_only=False, expected_dry_run=False)
        self.assertEqual(validate_summary["status"], "ok")
        self.assertEqual(apply_summary["status"], "ok")
        self.assertEqual(validate_summary["fatal_error"], None)
        self.assertEqual(apply_summary["fatal_error"], None)
        self.assertEqual(validate_summary["results"]["row_errors"], [])
        self.assertEqual(apply_summary["results"]["row_errors"], [])
        self.assertEqual(apply_summary["results"]["models"]["PlanningYear"]["created"], 2)
        self.assertEqual(apply_summary["results"]["models"]["Planting"]["created"], 2)
        self.assertEqual(apply_summary["results"]["models"]["SalesEvent"]["created"], 2)
        self.assertEqual(apply_summary["results"]["models"]["PackAllocation"]["created"], 2)
        self.assertEqual(sorted(PlanningYear.objects.values_list("year", flat=True)), [2021, 2023])

    def test_pack_allocation_recipe_requires_packed_quantity_when_recipe_name_present(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            recipe_name = "Carrot Mix Recipe"
            self._write_mix_recipe_pack_fixture(
                data_dir,
                year=2021,
                recipe_name=recipe_name,
                packed_quantity="",
                packed_unit="bag",
            )
            crop = CropInfo.objects.create(
                name="Carrot",
                crop_type="Vegetables",
                botanical_family="Apiaceae",
                propagation_type="seed",
                is_perennial=False,
                fresh_or_storage="fresh",
                storage_weeks=0,
                harvest_unit="pounds",
                avg_unit_weight="1.00",
                nursery_weeks=0,
                weeks_until_pot_up=0,
                seeds_per_cell=1,
                thinned_plants=0,
            )
            product = CropSalesFormat.objects.create(
                crop=crop,
                product_name="Carrot Bunch",
                sale_price="3.50",
                sale_unit="bag",
                harvest_qty_per_sale_unit="1.00",
                sku="CAR-BAG",
                is_active=True,
            )
            ProductRecipe.objects.create(
                product=product,
                name=recipe_name,
                output_unit="bag",
                is_active=True,
            )
            summary = self._run_import(data_dir, Path(output_dir) / "summary-mix-recipe-missing-packed-qty.json")

        errors = [
            err
            for err in summary["results"]["row_errors"]
            if err["field_path"] == "pack_allocations.packed_quantity"
        ]
        self.assertEqual(summary["status"], "ok")
        self.assertTrue(errors)
        self.assertEqual(errors[0]["code"], "missing_required")

    def test_pack_allocation_recipe_creates_pack_batch_and_links_sales_event(self):
        with TemporaryDirectory() as data_dir, TemporaryDirectory() as output_dir:
            recipe_name = "Carrot Mix Recipe"
            self._write_mix_recipe_pack_fixture(
                data_dir,
                year=2021,
                recipe_name=recipe_name,
                packed_quantity="12",
                packed_unit="bag",
                pack_date="2021-06-01",
            )
            crop = CropInfo.objects.create(
                name="Carrot",
                crop_type="Vegetables",
                botanical_family="Apiaceae",
                propagation_type="seed",
                is_perennial=False,
                fresh_or_storage="fresh",
                storage_weeks=0,
                harvest_unit="pounds",
                avg_unit_weight="1.00",
                nursery_weeks=0,
                weeks_until_pot_up=0,
                seeds_per_cell=1,
                thinned_plants=0,
            )
            product = CropSalesFormat.objects.create(
                crop=crop,
                product_name="Carrot Bunch",
                sale_price="3.50",
                sale_unit="bag",
                harvest_qty_per_sale_unit="1.00",
                sku="CAR-BAG",
                is_active=True,
            )
            ProductRecipe.objects.create(
                product=product,
                name=recipe_name,
                output_unit="bag",
                is_active=True,
            )
            summary = self._run_import(data_dir, Path(output_dir) / "summary-mix-recipe-pack-batch-link.json")

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(PackBatch.objects.count(), 1)
        pack_batch = PackBatch.objects.first()
        self.assertEqual(pack_batch.packed_quantity, Decimal("12"))
        self.assertEqual(pack_batch.packed_unit, "bag")
        linked_sale = SalesEvent.objects.get(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            sale_date=date(2021, 6, 1),
            product__product_name="Carrot Bunch",
        )
        self.assertEqual(linked_sale.pack_batch_id, pack_batch.id)


class StageA2OfflineConnectorTests(TestCase):
    def test_normalizer_detects_header_after_irregular_preamble_using_contract_scan(self):
        rows = [
            ["2026 crop plan export"],
            ["notes", "draft only"],
            [" block name ", "block type", "number of beds", "bed width feet", "bedfeet per bed"],
            ["Field 1", "Field", "10", "3", "100"],
        ]

        normalized = normalize_rows(
            rows,
            required_headers=["Block", "Block Type", "# of Beds", "Bed Width (feet)", "Bedfeet per Bed"],
            aliases={
                "block name": "Block",
                "block type": "Block Type",
                "number of beds": "# of Beds",
                "bed width feet": "Bed Width (feet)",
                "bedfeet per bed": "Bedfeet per Bed",
            },
        )

        self.assertEqual(normalized["header_row_index"], 2)
        self.assertEqual(normalized["strategy"], "required_header_set_scan")
        self.assertEqual(
            normalized["rows"][0],
            ["Block", "Block Type", "# of Beds", "Bed Width (feet)", "Bedfeet per Bed"],
        )
        self.assertEqual(normalized["rows"][1], ["Field 1", "Field", "10", "3", "100"])

    def test_normalizer_supports_anchor_token_fallback_when_header_aliases_are_insufficient(self):
        rows = [
            ["Instructions"],
            ["DATA START"],
            ["Beds", "Width", "Length"],
            ["Field 9", "4", "150"],
        ]

        normalized = normalize_rows(
            rows,
            required_headers=["Block", "Block Type", "# of Beds"],
            anchor_token="DATA START",
        )

        self.assertEqual(normalized["header_row_index"], 2)
        self.assertEqual(normalized["strategy"], "anchor_token")
        self.assertEqual(normalized["rows"][1], ["Field 9", "4", "150"])

    def test_normalizer_can_project_live_tab_columns_into_importer_contract(self):
        rows = [
            ["Choose Formats To Sell Your Crops"],
            [
                "Format",
                "Product",
                "Sale price",
                "Sale Units",
                "Harvest Qty",
                "Harvest Unit",
                "Product SKU",
            ],
            ["Arugula - 1/3 lb", "Arugula", "$7.00", "1/3 lb", "0.33", "pounds", "-1/3 lb"],
        ]

        normalized = normalize_rows(
            rows,
            required_headers=["Format", "Product", "Sale price", "Sale Units", "Harvest Qty", "Product SKU"],
            output_headers=[
                "Crop Name",
                "Product Name",
                "Sale Price",
                "Sale Unit",
                "Harvest Qty Per Sale Unit",
                "SKU",
                "Is Active",
            ],
            column_map={
                "Crop Name": "Product",
                "Product Name": "Format",
                "Sale Price": "Sale price",
                "Sale Unit": "Sale Units",
                "Harvest Qty Per Sale Unit": "Harvest Qty",
                "SKU": "Product SKU",
            },
            default_values={"Is Active": "true"},
        )

        self.assertEqual(normalized["header_row_index"], 1)
        self.assertEqual(
            normalized["rows"][0],
            [
                "Crop Name",
                "Product Name",
                "Sale Price",
                "Sale Unit",
                "Harvest Qty Per Sale Unit",
                "SKU",
                "Is Active",
            ],
        )
        self.assertEqual(
            normalized["rows"][1],
            ["Arugula", "Arugula - 1/3 lb", "$7.00", "1/3 lb", "0.33", "-1/3 lb", "true"],
        )

    def test_normalizer_can_transform_crop_planner_into_plantings_contract(self):
        rows = [
            ["Yellow Columns - Enter Your Information"],
            [
                "Crop // Variety",
                "Block",
                "Bed #",
                "Harvest Safety Factor",
                "Plan Field Year",
                "Plan Field Week",
                "Plan Bedft",
            ],
            ["Arugula // Astro", "B1", "11", "1.3", "2026", "15", "100"],
            ["Spinach // Corvair", "D1", "", "1.3", "2026", "20", "50"],
        ]

        normalized = normalize_rows(
            rows,
            required_headers=[
                "Crop // Variety",
                "Block",
                "Bed #",
                "Plan Field Year",
                "Plan Field Week",
                "Plan Bedft",
            ],
            output_headers=[
                "Crop",
                "Variety",
                "Block",
                "Bed Start",
                "Bed End",
                "Planned Plant Date",
                "Planned Bedfeet",
                "Status",
            ],
            column_map={
                "Crop": "Crop // Variety",
                "Variety": "Crop // Variety",
                "Block": "Block",
                "Bed Start": "Bed #",
                "Bed End": "Bed #",
                "Planned Bedfeet": "Plan Bedft",
            },
            default_values={"Status": "Planned"},
            row_transforms=[
                {
                    "type": "split",
                    "source": "Crop",
                    "delimiter": "//",
                    "left_target": "Crop",
                    "right_target": "Variety",
                },
                {
                    "type": "copy",
                    "source": "Bed Start",
                    "targets": ["Bed End"],
                },
                {
                    "type": "week_monday",
                    "year_source": "Plan Field Year",
                    "week_source": "Plan Field Week",
                    "target": "Planned Plant Date",
                },
            ],
        )

        self.assertEqual(normalized["header_row_index"], 1)
        self.assertEqual(
            normalized["rows"][0],
            [
                "Crop",
                "Variety",
                "Block",
                "Bed Start",
                "Bed End",
                "Planned Plant Date",
                "Planned Bedfeet",
                "Status",
            ],
        )
        self.assertEqual(
            normalized["rows"][1],
            ["Arugula", "Astro", "B1", "11", "11", "2026-04-06", "100", "Planned"],
        )
        self.assertEqual(
            normalized["rows"][2],
            ["Spinach", "Corvair", "D1", "", "", "2026-05-11", "50", "Planned"],
        )

    def test_normalizer_can_merge_multiple_source_regions_with_region_defaults(self):
        rows = [
            ["Choose Your Sales Channels"],
            ["CSA Channels"],
            ["Channel Name", "Days of the Week", "Start Week Num", "End Week Num", "$ Target per week"],
            ["Farm Share", "Tuesday", "1", "40", "300"],
            ["", "", "", "", ""],
            ["Other Channels"],
            ["Channel Name", "Days of the Week", "Start Week Num", "End Week Num", "$ Target per week"],
            ["Farm Stand", "Saturday", "1", "52", "500"],
        ]

        normalized = normalize_rows(
            rows,
            required_headers=[
                "Channel Name",
                "Days of the Week",
                "Start Week Num",
                "End Week Num",
                "$ Target per week",
            ],
            output_headers=[
                "Channel Name",
                "Days of the Week",
                "Start Week Num",
                "End Week Num",
                "$ Target per week",
                "is_csa",
                "Priority",
            ],
            column_map={
                "Channel Name": "Channel Name",
                "Days of the Week": "Days of the Week",
                "Start Week Num": "Start Week Num",
                "End Week Num": "End Week Num",
                "$ Target per week": "$ Target per week",
            },
            source_regions=[
                {
                    "anchor_token": "CSA Channels",
                    "default_values": {"is_csa": "true", "Priority": "100"},
                    "stop_on_blank_in": ["Channel Name"],
                    "prefer_anchor_token": True,
                },
                {
                    "anchor_token": "Other Channels",
                    "default_values": {"is_csa": "false", "Priority": "100"},
                    "stop_on_blank_in": ["Channel Name"],
                    "prefer_anchor_token": True,
                },
            ],
        )

        self.assertEqual(normalized["strategy"], "multi_region")
        self.assertEqual(normalized["header_row_indexes"], [2, 6])
        self.assertEqual(
            normalized["rows"],
            [
                [
                    "Channel Name",
                    "Days of the Week",
                    "Start Week Num",
                    "End Week Num",
                    "$ Target per week",
                    "is_csa",
                    "Priority",
                ],
                ["Farm Share", "Tuesday", "1", "40", "300", "true", "100"],
                ["Farm Stand", "Saturday", "1", "52", "500", "false", "100"],
            ],
        )

    def test_normalizer_grid_unpivot_wide_week_columns_to_product_week_plan(self):
        rows = [
            ["Annual sales draft"],
            ["Channel", "Product", "1", "2", "3"],
            ["Farm Stand", "Carrot Bunch", "10", "", "5"],
        ]
        normalized = normalize_rows(
            rows,
            required_headers=["Channel", "Product", "1"],
            grid_unpivot={
                "output_headers": [
                    "Channel Name",
                    "Product Name",
                    "Week",
                    "Planned Quantity",
                ],
                "identity_columns": [
                    {"output": "Channel Name", "source": "Channel"},
                    {"output": "Product Name", "source": "Product"},
                ],
                "skip_blank_quantity": True,
            },
        )
        self.assertEqual(
            normalized["rows"][0],
            ["Channel Name", "Product Name", "Week", "Planned Quantity"],
        )
        body = normalized["rows"][1:]
        self.assertEqual(
            sorted(body, key=lambda r: (int(r[2]), r[1])),
            [
                ["Farm Stand", "Carrot Bunch", "1", "10"],
                ["Farm Stand", "Carrot Bunch", "3", "5"],
            ],
        )

    def test_normalizer_grid_unpivot_accepts_week_prefixed_headers(self):
        rows = [
            ["Product", "Week 1", "Week 2"],
            ["Spinach lb", "4", "9"],
        ]
        normalized = normalize_rows(
            rows,
            required_headers=["Product", "Week 1"],
            grid_unpivot={
                "output_headers": [
                    "Channel Name",
                    "Product Name",
                    "Week",
                    "Planned Quantity",
                ],
                "identity_columns": [
                    {"output": "Channel Name", "fixed": "Farm Stand"},
                    {"output": "Product Name", "source": "Product"},
                ],
            },
        )
        body = normalized["rows"][1:]
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0], ["Farm Stand", "Spinach lb", "1", "4"])
        self.assertEqual(body[1], ["Farm Stand", "Spinach lb", "2", "9"])

    def test_normalizer_grid_unpivot_skips_rows_with_blank_product(self):
        rows = [
            ["Channel", "Product", "1"],
            ["Farm Stand", "", "99"],
            ["Farm Stand", "Okra lb", "3"],
        ]
        normalized = normalize_rows(
            rows,
            required_headers=["Channel", "Product", "1"],
            grid_unpivot={
                "output_headers": [
                    "Channel Name",
                    "Product Name",
                    "Week",
                    "Planned Quantity",
                ],
                "identity_columns": [
                    {"output": "Channel Name", "source": "Channel"},
                    {"output": "Product Name", "source": "Product"},
                ],
            },
        )
        self.assertEqual(len(normalized["rows"]), 2)

    def test_snapshot_stage_a2_bundle_command_writes_normalized_bundle_and_manifest(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            raw_dir = temp_path / "raw"
            raw_dir.mkdir()
            (raw_dir / "blocks-tab.csv").write_text(
                "\n".join(
                    [
                        "Farm export",
                        "Prepared by operations",
                        " block name ,block type,number of beds,bed width feet,bedfeet per bed",
                        "Field 1,Field,10,3,100",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = temp_path / "stage-a2-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "offline-fixture",
                        "tabs": [
                            {
                                "source_csv": "raw/blocks-tab.csv",
                                "output_path": "reference/blocks.csv",
                                "required_headers": [
                                    "Block",
                                    "Block Type",
                                    "# of Beds",
                                    "Bed Width (feet)",
                                    "Bedfeet per Bed",
                                ],
                                "aliases": {
                                    "block name": "Block",
                                    "block type": "Block Type",
                                    "number of beds": "# of Beds",
                                    "bed width feet": "Bed Width (feet)",
                                    "bedfeet per bed": "Bedfeet per Bed",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output_dir = temp_path / "bundle"
            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            with (output_dir / "reference" / "blocks.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.reader(handle))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(
            rows[0],
            ["Block", "Block Type", "# of Beds", "Bed Width (feet)", "Bedfeet per Bed"],
        )
        self.assertEqual(rows[1], ["Field 1", "Field", "10", "3", "100"])
        self.assertEqual(manifest["schema_version"], "a2-draft-1")
        self.assertEqual(manifest["source_id"], "offline-fixture")
        self.assertEqual(
            manifest["tabs"],
            [
                {
                    "header_row_index": 2,
                    "output_path": "reference/blocks.csv",
                    "rows_written": 1,
                    "source_csv": "raw/blocks-tab.csv",
                    "strategy": "required_header_set_scan",
                }
            ],
        )

    def test_snapshot_stage_a2_bundle_grid_unpivot_writes_product_week_plan(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            raw_dir = temp_path / "raw"
            raw_dir.mkdir()
            (raw_dir / "orders-wide.csv").write_text(
                "\n".join(
                    [
                        "Product,1,2,3",
                        "Carrot Bunch,10,,5",
                        "Arugula - 1/3 lb,,3,4",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = temp_path / "stage-a2-301.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "offline-301-sample",
                        "tabs": [
                            {
                                "source_csv": "raw/orders-wide.csv",
                                "output_path": "year_2026/product_week_plan.csv",
                                "required_headers": ["Product", "1", "2"],
                                "grid_unpivot": {
                                    "output_headers": [
                                        "Channel Name",
                                        "Product Name",
                                        "Week",
                                        "Planned Quantity",
                                    ],
                                    "identity_columns": [
                                        {"output": "Channel Name", "fixed": "Farm Stand"},
                                        {"output": "Product Name", "source": "Product"},
                                    ],
                                    "skip_blank_quantity": True,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output_dir = temp_path / "bundle"
            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )
            with (output_dir / "year_2026" / "product_week_plan.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(
            rows[0],
            ["Channel Name", "Product Name", "Week", "Planned Quantity"],
        )
        body = rows[1:]
        self.assertEqual(len(body), 4)
        self.assertIn(["Farm Stand", "Carrot Bunch", "1", "10"], body)
        self.assertIn(["Farm Stand", "Carrot Bunch", "3", "5"], body)
        self.assertIn(["Farm Stand", "Arugula - 1/3 lb", "2", "3"], body)
        self.assertIn(["Farm Stand", "Arugula - 1/3 lb", "3", "4"], body)

    def test_run_301_sales_plan_snapshot_rehearsal_script_writes_year_scoped_csv(self):
        repo_root = Path(__file__).resolve().parents[3]
        script = repo_root / "scripts" / "run_301_sales_plan_snapshot_rehearsal.py"
        self.assertTrue(script.is_file(), msg="expected repo script next to farm/")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "bundle"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--year",
                    "2027",
                    "--output-dir",
                    str(out),
                ],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            csv_path = out / "year_2027" / "product_week_plan.csv"
            self.assertTrue(csv_path.is_file())
            rows = csv_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(rows), 2)

    def test_snapshot_stage_a2_bundle_can_translate_live_reference_tab_shape(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            raw_dir = temp_path / "raw"
            raw_dir.mkdir()
            (raw_dir / "farm-crop-formats.csv").write_text(
                "\n".join(
                    [
                        "Choose Formats To Sell Your Crops",
                        "Format,Product,Sale price,Sale Units,Harvest Qty,Harvest Unit,Product SKU",
                        "Arugula - 1/3 lb,Arugula,$7.00,1/3 lb,0.33,pounds,-1/3 lb",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = temp_path / "stage-a2-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "offline-fixture",
                        "tabs": [
                            {
                                "source_csv": "raw/farm-crop-formats.csv",
                                "output_path": "reference/crop_sales_formats.csv",
                                "required_headers": [
                                    "Format",
                                    "Product",
                                    "Sale price",
                                    "Sale Units",
                                    "Harvest Qty",
                                    "Product SKU",
                                ],
                                "output_headers": [
                                    "Crop Name",
                                    "Product Name",
                                    "Sale Price",
                                    "Sale Unit",
                                    "Harvest Qty Per Sale Unit",
                                    "SKU",
                                    "Is Active",
                                ],
                                "column_map": {
                                    "Crop Name": "Product",
                                    "Product Name": "Format",
                                    "Sale Price": "Sale price",
                                    "Sale Unit": "Sale Units",
                                    "Harvest Qty Per Sale Unit": "Harvest Qty",
                                    "SKU": "Product SKU",
                                },
                                "default_values": {
                                    "Is Active": "true",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output_dir = temp_path / "bundle"
            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            with (output_dir / "reference" / "crop_sales_formats.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(
            rows,
            [
                [
                    "Crop Name",
                    "Product Name",
                    "Sale Price",
                    "Sale Unit",
                    "Harvest Qty Per Sale Unit",
                    "SKU",
                    "Is Active",
                ],
                ["Arugula", "Arugula - 1/3 lb", "$7.00", "1/3 lb", "0.33", "-1/3 lb", "true"],
            ],
        )

    def test_snapshot_stage_a2_bundle_can_translate_crop_planner_rows(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            raw_dir = temp_path / "raw"
            raw_dir.mkdir()
            (raw_dir / "crop-planner.csv").write_text(
                "\n".join(
                    [
                        "Yellow Columns - Enter Your Information",
                        "Crop // Variety,Block,Bed #,Harvest Safety Factor,Plan Field Year,Plan Field Week,Plan Bedft",
                        "Arugula // Astro,B1,11,1.3,2026,15,100",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = temp_path / "stage-a2-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "offline-fixture",
                        "tabs": [
                            {
                                "source_csv": "raw/crop-planner.csv",
                                "output_path": "year_2026/plantings.csv",
                                "required_headers": [
                                    "Crop // Variety",
                                    "Block",
                                    "Bed #",
                                    "Plan Field Year",
                                    "Plan Field Week",
                                    "Plan Bedft",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Variety",
                                    "Block",
                                    "Bed Start",
                                    "Bed End",
                                    "Planned Plant Date",
                                    "Planned Bedfeet",
                                    "Status",
                                ],
                                "column_map": {
                                    "Crop": "Crop // Variety",
                                    "Variety": "Crop // Variety",
                                    "Block": "Block",
                                    "Bed Start": "Bed #",
                                    "Bed End": "Bed #",
                                    "Planned Bedfeet": "Plan Bedft",
                                },
                                "default_values": {
                                    "Status": "Planned",
                                },
                                "row_transforms": [
                                    {
                                        "type": "split",
                                        "source": "Crop",
                                        "delimiter": "//",
                                        "left_target": "Crop",
                                        "right_target": "Variety",
                                    },
                                    {
                                        "type": "copy",
                                        "source": "Bed Start",
                                        "targets": ["Bed End"],
                                    },
                                    {
                                        "type": "week_monday",
                                        "year_source": "Plan Field Year",
                                        "week_source": "Plan Field Week",
                                        "target": "Planned Plant Date",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output_dir = temp_path / "bundle"
            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            with (output_dir / "year_2026" / "plantings.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(
            rows,
            [
                [
                    "Crop",
                    "Variety",
                    "Block",
                    "Bed Start",
                    "Bed End",
                    "Planned Plant Date",
                    "Planned Bedfeet",
                    "Status",
                ],
                ["Arugula", "Astro", "B1", "11", "11", "2026-04-06", "100", "Planned"],
            ],
        )

    def test_snapshot_stage_a2_bundle_can_merge_multi_region_sales_channel_tables(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            raw_dir = temp_path / "raw"
            raw_dir.mkdir()
            (raw_dir / "sales-channels.csv").write_text(
                "\n".join(
                    [
                        "Choose Your Sales Channels",
                        "CSA Channels",
                        "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week",
                        "Farm Share,Tuesday,1,40,300",
                        ",,,,",
                        "Other Channels",
                        "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week",
                        "Farm Stand,Saturday,1,52,500",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = temp_path / "stage-a2-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "offline-fixture",
                        "tabs": [
                            {
                                "source_csv": "raw/sales-channels.csv",
                                "output_path": "reference/sales_channels.csv",
                                "required_headers": [
                                    "Channel Name",
                                    "Days of the Week",
                                    "Start Week Num",
                                    "End Week Num",
                                    "$ Target per week",
                                ],
                                "output_headers": [
                                    "Channel Name",
                                    "Days of the Week",
                                    "Start Week Num",
                                    "End Week Num",
                                    "$ Target per week",
                                    "is_csa",
                                    "Priority",
                                ],
                                "column_map": {
                                    "Channel Name": "Channel Name",
                                    "Days of the Week": "Days of the Week",
                                    "Start Week Num": "Start Week Num",
                                    "End Week Num": "End Week Num",
                                    "$ Target per week": "$ Target per week",
                                },
                                "source_regions": [
                                    {
                                        "anchor_token": "CSA Channels",
                                        "default_values": {"is_csa": "true", "Priority": "100"},
                                        "stop_on_blank_in": ["Channel Name"],
                                        "prefer_anchor_token": True,
                                    },
                                    {
                                        "anchor_token": "Other Channels",
                                        "default_values": {"is_csa": "false", "Priority": "100"},
                                        "stop_on_blank_in": ["Channel Name"],
                                        "prefer_anchor_token": True,
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output_dir = temp_path / "bundle"
            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            with (output_dir / "reference" / "sales_channels.csv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(
            rows,
            [
                [
                    "Channel Name",
                    "Days of the Week",
                    "Start Week Num",
                    "End Week Num",
                    "$ Target per week",
                    "is_csa",
                    "Priority",
                ],
                ["Farm Share", "Tuesday", "1", "40", "300", "true", "100"],
                ["Farm Stand", "Saturday", "1", "52", "500", "false", "100"],
            ],
        )


class ImportReferenceDataCommandTests(TestCase):
    def _write_csv(self, data_dir, name, lines):
        Path(data_dir, name).write_text("\n".join(lines), encoding="utf-8")

    def test_import_channels_continues_after_malformed_row(self):
        with TemporaryDirectory() as data_dir:
            self._write_csv(
                data_dir,
                "sales_channels.csv",
                [
                    "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week,is_csa,Priority",
                    "Farm Stand,Saturday,1,52,$500,false,1",
                    "Broken Channel,Friday,1,52,$not-a-number,false,2",
                    "CSA,Tuesday + Friday,1,40,$250,true,3",
                ],
            )
            call_command("import_reference_data", data_dir)
            self.assertEqual(SalesChannel.objects.count(), 2)


class PrimaryRouteSmokeTests(TestCase):
    REQUIRED_NAMESPACES = {"core", "reference", "planning", "operations", "sales", "reports"}
    MIN_REGISTERED_ROUTES_BY_NAMESPACE = {
        "core": 3,
        "reference": 1,
        "planning": 17,
        "operations": 8,
        "sales": 3,
        "reports": 14,
    }
    CRITICAL_ROUTES = [
        ("core:dashboard", {}),
        ("core:clone_plan_ui", {"source_year": 2026}),
        ("core:complete_season", {}),
        ("planning:matrix", {}),
        ("planning:planting_create", {}),
        ("planning:planting_detail", {"pk": 1}),
        ("planning:planting_edit", {"pk": 1}),
        ("planning:planting_status", {"pk": 1}),
        ("planning:nursery_schedule", {}),
        ("planning:harvest_calendar", {}),
        ("planning:field_schedule", {}),
        ("operations:harvest_entry_current", {}),
        ("operations:harvest_entry_week", {"week": 12}),
        ("operations:harvest_entry", {"pk": 1}),
        ("operations:field_walk_current", {}),
        ("operations:field_walk", {"pk": 1}),
        ("operations:inventory", {}),
        ("operations:inventory_add", {}),
        ("sales:market_entry", {}),
        ("sales:market_entry_channel", {"channel_id": 1}),
        ("sales:market_entry_date", {"channel_id": 1, "sale_date": "2026-03-15"}),
        ("reports:crop_map", {}),
        ("reports:crop_performance", {}),
        ("reports:seed_order", {}),
        ("reports:season_summary", {}),
        ("reports:plan_vs_actual", {}),
    ]
    PRIMARY_ROUTES = [
        ("core:dashboard", {}),
        ("reference:index", {}),
        ("planning:matrix", {}),
        ("planning:matrix_date", {"date": "2026-03-15"}),
        ("planning:matrix_week", {"week": 12}),
        ("planning:nursery_schedule", {}),
        ("planning:nursery_week", {"week": 12}),
        ("planning:harvest_calendar", {}),
        ("operations:harvest_entry_current", {}),
        ("operations:harvest_entry_week", {"week": 12}),
        ("operations:field_walk_current", {}),
        ("operations:inventory", {}),
        ("sales:market_entry", {}),
        ("sales:market_entry_channel", {"channel_id": 1}),
        ("sales:market_entry_date", {"channel_id": 1, "sale_date": "2026-03-15"}),
        ("reports:crop_map", {}),
        ("reports:crop_map_week", {"week": 12}),
        ("reports:harvest_list_print", {"week": 12}),
        ("reports:pack_list_print", {"week": 12}),
        ("reports:weekly_schedule_print", {"week": 12}),
        ("reports:nursery_schedule_print", {}),
        ("reports:seed_order", {}),
        ("reports:crop_performance", {}),
        ("reports:channel_performance", {}),
        ("reports:block_utilization", {}),
        ("reports:season_summary", {}),
        ("reports:plan_vs_actual", {}),
    ]
    MIN_PRIMARY_SMOKE_ROUTES_BY_NAMESPACE = {
        "core": 1,
        "reference": 1,
        "planning": 6,
        "operations": 4,
        "sales": 3,
        "reports": 12,
    }
    EXPECTED_PRIMARY_SMOKE_ROUTES_BY_NAMESPACE = {
        "core": 1,
        "reference": 1,
        "planning": 6,
        "operations": 4,
        "sales": 3,
        "reports": 12,
    }

    @classmethod
    def setUpTestData(cls):
        PlanningYear.objects.create(year=2026, status="active")
        SalesChannel.objects.create(
            name="Farm Stand",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target="500.00",
            is_csa=False,
            allocation_priority=1,
        )

    def test_primary_navigation_routes_do_not_raise_server_errors(self):
        for route_name, kwargs in self.PRIMARY_ROUTES:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name, kwargs=kwargs))
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers.get("Content-Type", ""))

    def test_primary_navigation_routes_cover_required_namespaces(self):
        route_namespaces = {route_name.split(":")[0] for route_name, _ in self.PRIMARY_ROUTES}
        self.assertEqual(route_namespaces, self.REQUIRED_NAMESPACES)

    def test_primary_navigation_routes_meet_smoke_breadth_by_namespace(self):
        namespace_counts = {namespace: 0 for namespace in self.REQUIRED_NAMESPACES}
        for route_name, _kwargs in self.PRIMARY_ROUTES:
            namespace = route_name.split(":")[0]
            namespace_counts[namespace] += 1
        self.assertEqual(set(namespace_counts.keys()), self.REQUIRED_NAMESPACES)
        for namespace, min_count in self.MIN_PRIMARY_SMOKE_ROUTES_BY_NAMESPACE.items():
            with self.subTest(namespace=namespace):
                self.assertGreaterEqual(namespace_counts[namespace], min_count)
        self.assertEqual(namespace_counts, self.EXPECTED_PRIMARY_SMOKE_ROUTES_BY_NAMESPACE)

    def test_primary_navigation_routes_are_unique_for_stable_smoke_gate_surface(self):
        normalized_routes = {
            (route_name, tuple(sorted(kwargs.items()))) for route_name, kwargs in self.PRIMARY_ROUTES
        }
        self.assertEqual(len(self.PRIMARY_ROUTES), len(normalized_routes))

    def test_registered_route_surface_meets_minimum_route_breadth_by_namespace(self):
        from core import urls as core_urls
        from operations import urls as operations_urls
        from planning import urls as planning_urls
        from reference import urls as reference_urls
        from reports import urls as reports_urls
        from sales import urls as sales_urls

        namespace_counts = {
            "core": len(core_urls.urlpatterns),
            "reference": len(reference_urls.urlpatterns),
            "planning": len(planning_urls.urlpatterns),
            "operations": len(operations_urls.urlpatterns),
            "sales": len(sales_urls.urlpatterns),
            "reports": len(reports_urls.urlpatterns),
        }
        self.assertEqual(set(namespace_counts.keys()), self.REQUIRED_NAMESPACES)
        for namespace, min_count in self.MIN_REGISTERED_ROUTES_BY_NAMESPACE.items():
            with self.subTest(namespace=namespace):
                self.assertGreaterEqual(namespace_counts.get(namespace, 0), min_count)

    def test_primary_navigation_routes_include_named_urls_for_expected_surface(self):
        for route_name, kwargs in self.CRITICAL_ROUTES:
            with self.subTest(route=route_name):
                resolved = reverse(route_name, kwargs=kwargs)
                self.assertTrue(resolved)

    def test_primary_navigation_routes_support_head_without_server_errors(self):
        for route_name, kwargs in self.PRIMARY_ROUTES:
            with self.subTest(route=route_name):
                response = self.client.head(reverse(route_name, kwargs=kwargs))
                self.assertLess(response.status_code, 500)
                self.assertNotEqual(response.status_code, 404)

    def test_primary_navigation_routes_accept_query_params_without_server_errors(self):
        for route_name, kwargs in self.PRIMARY_ROUTES:
            with self.subTest(route=route_name):
                route = reverse(route_name, kwargs=kwargs)
                response = self.client.get(f"{route}?smoke_gate=1")
                self.assertLess(response.status_code, 500)
                self.assertNotEqual(response.status_code, 404)

    def test_admin_route_redirects_to_login_boundary(self):
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_admin_login_boundary_is_reachable(self):
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<form", status_code=200)

    def test_unknown_route_baseline_returns_404_for_get_head_and_query_variant(self):
        route_variants = [
            "/definitely-not-a-real-route/",
            "/definitely-not-a-real-route",
            "/planning/definitely-not-a-real-route/",
            "/operations/definitely-not-a-real-route/",
            "/sales/definitely-not-a-real-route/",
            "/reports/definitely-not-a-real-route/",
        ]
        query_variants = [
            "?smoke_gate=1",
            "?smoke_gate=1&depth=alpha-internal",
            "?smoke_gate=true&smoke_gate=false",
        ]
        for route in route_variants:
            with self.subTest(route=route, method="GET"):
                self.assertEqual(self.client.get(route).status_code, 404)
            with self.subTest(route=route, method="HEAD"):
                self.assertEqual(self.client.head(route).status_code, 404)
            for query in query_variants:
                with self.subTest(route=route, method="GET+query", query=query):
                    self.assertEqual(self.client.get(f"{route}{query}").status_code, 404)
                with self.subTest(route=route, method="HEAD+query", query=query):
                    self.assertEqual(self.client.head(f"{route}{query}").status_code, 404)

    @override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
    def test_primary_smoke_suite_includes_app_boot_checks(self):
        # Keep app boot assertions in the same suite operators already run for route smoke gates.
        call_command("check")
        resolver = get_resolver()
        namespace_dict = getattr(resolver, "namespace_dict", {})
        self.assertTrue(namespace_dict)
        self.assertTrue(self.REQUIRED_NAMESPACES <= set(namespace_dict))


class AppBootGateTests(TestCase):
    @override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
    def test_django_check_passes_for_release_gate(self):
        call_command("check")

    @override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
    def test_root_urlconf_loads_and_exposes_expected_namespaces(self):
        resolver = get_resolver()
        namespace_dict = getattr(resolver, "namespace_dict", {})
        self.assertTrue(namespace_dict)
        self.assertTrue({"core", "reference", "planning", "operations", "sales", "reports"} <= set(namespace_dict))

    @override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
    def test_healthz_endpoint_reports_process_ok(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["check"], "healthz")

    @override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
    def test_readyz_endpoint_reports_db_and_urlconf_readiness(self):
        response = self.client.get("/readyz/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["check"], "readyz")
        self.assertEqual(payload["checks"]["db"], "ok")
        self.assertEqual(payload["checks"]["urlconf"], "ok")

    @override_settings(DEBUG=False, SECRET_KEY="dev-insecure-key", ALLOWED_HOSTS=["farm.example.com"])
    def test_production_settings_check_rejects_insecure_secret_key(self):
        with patch("sys.argv", ["manage.py", "check"]):
            with self.assertRaises(SystemCheckError) as exc_info:
                call_command("check")
        self.assertIn("core.E001", str(exc_info.exception))

    @override_settings(DEBUG=False, SECRET_KEY="strong-secret-key", ALLOWED_HOSTS=["*"])
    def test_production_settings_check_rejects_wildcard_allowed_hosts(self):
        with patch("sys.argv", ["manage.py", "check"]):
            with self.assertRaises(SystemCheckError) as exc_info:
                call_command("check")
        self.assertIn("core.E003", str(exc_info.exception))

    @override_settings(DEBUG=False, SECRET_KEY="strong-secret-key", ALLOWED_HOSTS=[])
    def test_production_settings_check_rejects_empty_allowed_hosts(self):
        with patch("sys.argv", ["manage.py", "check"]):
            with self.assertRaises(SystemCheckError) as exc_info:
                call_command("check")
        self.assertIn("core.E002", str(exc_info.exception))

    @override_settings(DEBUG=False, SECRET_KEY="dev-insecure-key", ALLOWED_HOSTS=[])
    def test_production_settings_check_reports_multiple_guardrail_failures(self):
        with patch("sys.argv", ["manage.py", "check"]):
            with self.assertRaises(SystemCheckError) as exc_info:
                call_command("check")
        self.assertIn("core.E001", str(exc_info.exception))
        self.assertIn("core.E002", str(exc_info.exception))

    @override_settings(DEBUG=False, SECRET_KEY="strong-secret-key", ALLOWED_HOSTS=["farm.example.com"])
    def test_production_settings_check_accepts_explicit_hosts_and_strong_secret(self):
        with patch("sys.argv", ["manage.py", "check"]):
            call_command("check")


class PlanningYearResolutionTests(TestCase):
    def test_resolver_prefers_active_then_planning(self):
        PlanningYear.objects.create(year=2025, status="planning")
        PlanningYear.objects.create(year=2026, status="active")

        year_obj = resolve_current_planning_year()

        self.assertIsNotNone(year_obj)
        self.assertEqual(year_obj.status, "active")
        self.assertEqual(year_obj.year, 2026)

    def test_resolver_falls_back_to_latest_year_when_enabled(self):
        PlanningYear.objects.create(year=2024, status="complete")
        PlanningYear.objects.create(year=2025, status="complete")

        year_obj = resolve_current_planning_year(fallback_latest=True)

        self.assertIsNotNone(year_obj)
        self.assertEqual(year_obj.year, 2025)

    def test_resolver_returns_none_without_matching_status_or_fallback(self):
        PlanningYear.objects.create(year=2024, status="complete")

        year_obj = resolve_current_planning_year(status_priority=("active", "planning"))

        self.assertIsNone(year_obj)


class DomainModelInvariantTests(TestCase):
    def test_crop_by_season_computed_metrics_are_deterministic(self):
        crop = CropInfo.objects.create(
            name="Storage Beet",
            crop_type="Roots",
            botanical_family="Amaranthaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=10,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        profile = CropBySeason.objects.create(
            crop=crop,
            block_type="field",
            field_week_start=12,
            field_week_end=30,
            total_yield_per_bedfoot=Decimal("1.50"),
            harvest_weeks=6,
            dtm_days=65,
            rows_per_bed=3,
        )

        self.assertEqual(profile.wtm_weeks, 10)
        self.assertEqual(profile.weekly_yield_per_bedfoot, Decimal("0.25"))

    def test_sales_channel_wraparound_weeks_and_annual_target_are_deterministic(self):
        wrap_channel = SalesChannel.objects.create(
            name="Winter CSA",
            days_of_week=["Tuesday"],
            start_week=48,
            end_week=4,
            weekly_target=Decimal("125.00"),
            is_csa=True,
            allocation_priority=2,
        )
        standard_channel = SalesChannel.objects.create(
            name="Farm Stand",
            days_of_week=["Saturday"],
            start_week=10,
            end_week=20,
            weekly_target=Decimal("200.00"),
            is_csa=False,
            allocation_priority=1,
        )

        self.assertEqual(wrap_channel.num_weeks, 9)
        self.assertEqual(wrap_channel.annual_target, Decimal("1125.00"))
        self.assertEqual(standard_channel.num_weeks, 11)
        self.assertEqual(standard_channel.annual_target, Decimal("2200.00"))

    def test_sales_event_sell_through_pct_uses_actual_quantity_then_return_fallback(self):
        crop = CropInfo.objects.create(
            name="Carrot",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="fresh",
            storage_weeks=0,
            harvest_unit="bunches",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        product = CropSalesFormat.objects.create(
            crop=crop,
            product_name="Carrot Bunch",
            sale_price=Decimal("3.50"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            is_active=True,
        )
        channel = SalesChannel.objects.create(
            name="Farm Stand",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("300.00"),
            is_csa=False,
            allocation_priority=1,
        )

        explicit_actual = SalesEvent.objects.create(
            channel=channel,
            sale_date=date(2026, 6, 14),
            product=product,
            actual_quantity=Decimal("6.00"),
            brought_quantity=Decimal("10.00"),
            returned_quantity=Decimal("4.00"),
        )
        fallback_actual = SalesEvent.objects.create(
            channel=channel,
            sale_date=date(2026, 6, 21),
            product=product,
            actual_quantity=None,
            brought_quantity=Decimal("10.00"),
            returned_quantity=Decimal("3.00"),
        )

        self.assertEqual(explicit_actual.sell_through_pct, Decimal("60.0"))
        self.assertEqual(fallback_actual.sell_through_pct, Decimal("70.0"))
        self.assertEqual(explicit_actual.sale_week, 24)

    def test_quick_sales_entry_total_revenue_sums_cash_and_card(self):
        channel = SalesChannel.objects.create(
            name="Market",
            days_of_week=["Sunday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("150.00"),
            is_csa=False,
            allocation_priority=1,
        )
        quick_entry = QuickSalesEntry.objects.create(
            channel=channel,
            sale_date=date(2026, 6, 14),
            total_cash=Decimal("120.00"),
            total_card=Decimal("80.50"),
        )

        self.assertEqual(quick_entry.total_revenue, Decimal("200.50"))


class BetaGateEvidenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fixture_root = Path(__file__).resolve().parents[2] / "data" / "import_fixtures"

    def _bootstrap_core_workflow_records(self):
        planning_year = PlanningYear.objects.create(year=2026, status="active")
        block = Block.objects.create(
            name="Field 1",
            block_type="field",
            num_beds=10,
            bed_width_feet="3.0",
            bedfeet_per_bed=100,
        )
        crop = CropInfo.objects.create(
            name="Carrot",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=12,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        crop_season = CropBySeason.objects.create(
            crop=crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot=Decimal("1.20"),
            harvest_weeks=6,
            dtm_days=65,
            rows_per_bed=3,
        )
        planting = Planting.objects.create(
            planning_year=planning_year,
            crop=crop,
            crop_season=crop_season,
            block=block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=date(2026, 4, 1),
            status="planned",
        )
        channel = SalesChannel.objects.create(
            name="Farm Stand",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target="500.00",
            is_csa=False,
            allocation_priority=1,
        )
        return planting, channel

    def _run_import(self, data_dir, summary_path, *extra_args):
        call_command("import_historical_data", data_dir, "--summary-json", str(summary_path), *extra_args)
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def _assert_summary_contract(self, summary, expected_validate_only, expected_dry_run=False):
        self.assertIn(summary["schema_version"], {"1.1", "1.2", "1.3"})
        self.assertIn(summary["status"], {"ok", "failed"})
        self.assertEqual(summary["run"]["validate_only"], expected_validate_only)
        self.assertEqual(summary["run"]["dry_run"], expected_dry_run)
        self.assertIn("atomic_apply", summary["run"])
        self.assertTrue({"models", "totals", "row_errors"} <= set(summary["results"].keys()))

    def _assert_failure_signature_payload_shape(self, failure_signatures):
        for item in failure_signatures:
            with self.subTest(signature=item.get("signature")):
                self.assertTrue(item["signature"])
                self.assertGreaterEqual(item["count"], 1)
                self.assertTrue(item["owner_area"])
                self.assertTrue(item["owner_team"])
                self.assertIn(item["severity"], {"high", "medium"})
                self.assertTrue(item["escalation_path"])
                self.assertTrue(item["recovery"])

    def _authenticate_operator(self):
        user = get_user_model().objects.create_user(
            username="beta-operator",
            email="beta-operator@example.com",
            password="test-pass-123",
            is_staff=True,
        )
        self.client.force_login(user)
        return user

    def _post_planting_status(self, planting, status):
        return self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": status},
        )

    def _authenticate_non_staff_user(self):
        user = get_user_model().objects.create_user(
            username="beta-viewer",
            email="beta-viewer@example.com",
            password="test-pass-123",
            is_staff=False,
        )
        self.client.force_login(user)
        return user

    def test_critical_workflow_integration_path_persists_planning_operations_and_sales_records(self):
        planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        status_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(status_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "planted")

        inventory_response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": planting.crop_id,
                "event_type": "return_in",
                "quantity": "8.50",
                "notes": "beta gate critical workflow check",
            },
        )
        self.assertEqual(inventory_response.status_code, 302)
        ledger_entry = InventoryLedger.objects.get(crop_id=planting.crop_id)
        self.assertEqual(str(ledger_entry.quantity), "8.50")

        sales_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                "total_cash": "120.00",
                "total_card": "80.00",
                "notes": "beta gate critical workflow check",
            },
        )
        self.assertEqual(sales_response.status_code, 302)
        self.assertEqual(QuickSalesEntry.objects.count(), 1)
        self.assertEqual(QuickSalesEntry.objects.get().channel_id, channel.id)

    def test_importer_apply_repeated_runs_remain_idempotent_after_initial_write(self):
        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            first_summary = self._run_import(str(fixture_dir), Path(output_dir) / "summary-repeat-first.json")
            second_summary = self._run_import(str(fixture_dir), Path(output_dir) / "summary-repeat-second.json")
            third_summary = self._run_import(str(fixture_dir), Path(output_dir) / "summary-repeat-third.json")

        self.assertGreater(first_summary["results"]["totals"]["created"], 0)
        self.assertEqual(second_summary["results"]["totals"]["created"], 0)
        self.assertEqual(third_summary["results"]["totals"]["created"], 0)
        self.assertEqual(second_summary["results"]["totals"]["updated"], third_summary["results"]["totals"]["updated"])
        self.assertEqual(second_summary["results"]["totals"]["error"], third_summary["results"]["totals"]["error"])
        self.assertEqual(second_summary["results"]["row_errors"], third_summary["results"]["row_errors"])
        self.assertEqual(
            second_summary["results"]["failure_signatures"],
            third_summary["results"]["failure_signatures"],
        )

    def test_mismatch_preflight_emits_expected_failure_signature_aggregate_evidence(self):
        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-preflight-signatures.json",
                "--preflight",
            )

        self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
        signature_counts = {
            item["signature"]: item["count"] for item in summary["results"]["failure_signatures"]
        }
        self.assertEqual(set(signature_counts.keys()), {"namespace_mismatch", "stale_fk"})
        self.assertEqual(signature_counts["namespace_mismatch"], 4)
        self.assertEqual(signature_counts["stale_fk"], 11)
        self.assertEqual(Block.objects.count(), 0)
        self.assertEqual(CropInfo.objects.count(), 0)
        self.assertEqual(PlanningYear.objects.count(), 0)

    def test_mismatch_validate_only_and_apply_have_identical_failure_signature_counts(self):
        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            validate_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-signatures-validate.json",
                "--validate-only",
            )
            apply_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-signatures-apply.json",
            )

        validate_pairs = sorted(
            (item["signature"], item["count"]) for item in validate_summary["results"]["failure_signatures"]
        )
        apply_pairs = sorted(
            (item["signature"], item["count"]) for item in apply_summary["results"]["failure_signatures"]
        )
        self._assert_failure_signature_payload_shape(validate_summary["results"]["failure_signatures"])
        self._assert_failure_signature_payload_shape(apply_summary["results"]["failure_signatures"])
        self.assertEqual(validate_pairs, apply_pairs)
        self.assertEqual(validate_summary["results"]["totals"]["created"], 0)
        self.assertGreater(apply_summary["results"]["totals"]["created"], 0)

    def test_mismatch_preflight_alias_matches_validate_only_full_gate_payload(self):
        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            validate_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-validate-only-full-gate.json",
                "--validate-only",
            )
            preflight_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-preflight-full-gate.json",
                "--preflight",
            )

        self._assert_summary_contract(validate_summary, expected_validate_only=True, expected_dry_run=False)
        self._assert_summary_contract(preflight_summary, expected_validate_only=True, expected_dry_run=False)
        self.assertEqual(validate_summary["status"], preflight_summary["status"])
        self.assertEqual(validate_summary["fatal_error"], preflight_summary["fatal_error"])
        self.assertEqual(validate_summary["results"]["totals"], preflight_summary["results"]["totals"])
        self.assertEqual(validate_summary["results"]["models"], preflight_summary["results"]["models"])
        self.assertEqual(validate_summary["results"]["row_errors"], preflight_summary["results"]["row_errors"])
        self.assertEqual(
            validate_summary["results"]["failure_signatures"],
            preflight_summary["results"]["failure_signatures"],
        )
        self.assertEqual(
            validate_summary["results"]["escalation_summary"],
            preflight_summary["results"]["escalation_summary"],
        )

    def test_mismatch_repeated_preflight_runs_emit_stable_escalation_summary(self):
        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            first_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-escalation-first.json",
                "--validate-only",
            )
            second_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-escalation-second.json",
                "--validate-only",
            )
            third_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-escalation-third.json",
                "--validate-only",
            )

        self.assertEqual(
            first_summary["results"]["escalation_summary"],
            second_summary["results"]["escalation_summary"],
        )
        self.assertEqual(
            second_summary["results"]["escalation_summary"],
            third_summary["results"]["escalation_summary"],
        )

    def test_mismatch_preflight_is_readonly_against_existing_seed_data(self):
        CropInfo.objects.create(
            name="Baseline Crop",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="fresh",
            storage_weeks=0,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        Block.objects.create(
            name="Baseline Block",
            block_type="field",
            num_beds=4,
            bed_width_feet="3.0",
            bedfeet_per_bed=50,
        )
        baseline_counts = {
            "blocks": Block.objects.count(),
            "crops": CropInfo.objects.count(),
            "crop_seasons": CropBySeason.objects.count(),
            "channels": SalesChannel.objects.count(),
            "products": CropSalesFormat.objects.count(),
            "years": PlanningYear.objects.count(),
            "plantings": Planting.objects.count(),
        }

        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-preflight-readonly-seeded.json",
                "--preflight",
            )

        self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
        self.assertEqual(summary["results"]["totals"]["error"], 15)
        self.assertEqual(summary["results"]["totals"]["created"], 0)
        self.assertEqual(
            baseline_counts,
            {
                "blocks": Block.objects.count(),
                "crops": CropInfo.objects.count(),
                "crop_seasons": CropBySeason.objects.count(),
                "channels": SalesChannel.objects.count(),
                "products": CropSalesFormat.objects.count(),
                "years": PlanningYear.objects.count(),
                "plantings": Planting.objects.count(),
            },
        )

    def test_failure_signatures_contract_maps_each_import_error_to_owner_and_escalation(self):
        fixture_dir = self.fixture_root / "mismatch"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        signature_contracts = manifest["expected"]["failure_signatures"]
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-failure-signature-contract.json",
                "--validate-only",
            )

        self._assert_summary_contract(summary, expected_validate_only=True, expected_dry_run=False)
        seen_signatures = set()
        for row_error in summary["results"]["row_errors"]:
            signature = f"{row_error['code']}:{row_error['field_path']}"
            with self.subTest(signature=signature):
                self.assertIn(signature, signature_contracts)
                contract = signature_contracts[signature]
                self.assertTrue(contract["owner_area"])
                self.assertTrue(contract["escalation_path"])
                self.assertIn(contract["severity"], {"high", "medium"})
                seen_signatures.add(signature)

        self.assertEqual(seen_signatures, set(signature_contracts.keys()))

        emitted_signatures = {item["signature"] for item in summary["results"]["failure_signatures"]}
        self.assertEqual(emitted_signatures, {"namespace_mismatch", "stale_fk"})
        self._assert_failure_signature_payload_shape(summary["results"]["failure_signatures"])

    def test_mismatch_apply_repeats_preserve_reference_and_planning_model_counts(self):
        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            self._run_import(str(fixture_dir), Path(output_dir) / "summary-mismatch-apply-first-counts.json")
            counts_after_first = {
                "blocks": Block.objects.count(),
                "crops": CropInfo.objects.count(),
                "crop_seasons": CropBySeason.objects.count(),
                "channels": SalesChannel.objects.count(),
                "products": CropSalesFormat.objects.count(),
                "years": PlanningYear.objects.count(),
                "plantings": Planting.objects.count(),
            }
            second_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-apply-second-counts.json",
            )
            third_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-apply-third-counts.json",
            )

        self.assertEqual(second_summary["results"]["totals"]["created"], 0)
        self.assertEqual(third_summary["results"]["totals"]["created"], 0)
        self.assertEqual(
            counts_after_first,
            {
                "blocks": Block.objects.count(),
                "crops": CropInfo.objects.count(),
                "crop_seasons": CropBySeason.objects.count(),
                "channels": SalesChannel.objects.count(),
                "products": CropSalesFormat.objects.count(),
                "years": PlanningYear.objects.count(),
                "plantings": Planting.objects.count(),
            },
        )

    def test_critical_workflow_integration_keeps_operational_views_reachable_after_writes(self):
        planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()
        self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": planting.crop_id,
                "event_type": "return_in",
                "quantity": "5.00",
                "notes": "gate workflow views smoke",
            },
        )
        self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                "total_cash": "90.00",
                "total_card": "40.00",
                "notes": "gate workflow views smoke",
            },
        )

        for route_name, kwargs in [
            ("operations:inventory", {}),
            ("sales:market_entry_date", {"channel_id": channel.id, "sale_date": "2026-06-14"}),
            ("reports:season_summary", {}),
        ]:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name, kwargs=kwargs))
                self.assertEqual(response.status_code, 200)

    def test_import_seeded_workflow_path_supports_planning_operations_and_sales_posts(self):
        fixture_dir = self.fixture_root / "clean"
        with TemporaryDirectory() as output_dir:
            summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-clean-apply-critical-workflow-seed.json",
            )

        self._assert_summary_contract(summary, expected_validate_only=False, expected_dry_run=False)
        self.assertEqual(summary["status"], "ok")

        seeded_block = Block.objects.get(name="Field 1")
        seeded_crop = CropInfo.objects.create(
            name="Carrot",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=12,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        seeded_crop_season = CropBySeason.objects.create(
            crop=seeded_crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot=Decimal("1.20"),
            harvest_weeks=6,
            dtm_days=65,
            rows_per_bed=3,
        )
        seeded_channel = SalesChannel.objects.create(
            name="Farm Stand",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target="500.00",
            is_csa=False,
            allocation_priority=1,
        )
        planning_year = PlanningYear.objects.create(year=2027, status="active")
        planting = Planting.objects.create(
            planning_year=planning_year,
            crop=seeded_crop,
            crop_season=seeded_crop_season,
            block=seeded_block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=date(2027, 4, 1),
            status="planned",
        )
        channel = seeded_channel
        self._authenticate_operator()

        status_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(status_response.status_code, 302)

        inventory_response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": planting.crop_id,
                "event_type": "return_in",
                "quantity": "6.00",
                "notes": "import-seeded workflow check",
            },
        )
        self.assertEqual(inventory_response.status_code, 302)
        self.assertTrue(InventoryLedger.objects.filter(crop_id=planting.crop_id).exists())

        sales_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2027-06-07",
                "total_cash": "75.00",
                "total_card": "25.00",
                "notes": "import-seeded workflow check",
            },
        )
        self.assertEqual(sales_response.status_code, 302)
        self.assertEqual(QuickSalesEntry.objects.count(), 1)

    def test_anonymous_mutations_redirect_to_admin_login_boundary(self):
        planting, channel = self._bootstrap_core_workflow_records()
        inventory_crop = planting.crop

        status_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(status_response.status_code, 302)
        self.assertIn("/admin/login/", status_response["Location"])

        inventory_response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": inventory_crop.id,
                "event_type": "return_in",
                "quantity": "2.00",
                "notes": "anonymous boundary check",
            },
        )
        self.assertEqual(inventory_response.status_code, 302)
        self.assertIn("/admin/login/", inventory_response["Location"])

        sales_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                "total_cash": "10.00",
                "total_card": "5.00",
                "notes": "anonymous boundary check",
            },
        )
        self.assertEqual(sales_response.status_code, 302)
        self.assertIn("/admin/login/", sales_response["Location"])

    def test_authenticated_non_staff_mutations_are_forbidden_for_critical_write_routes(self):
        planting, channel = self._bootstrap_core_workflow_records()
        inventory_crop = planting.crop
        self._authenticate_non_staff_user()

        status_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(status_response.status_code, 403)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "planned")

        inventory_response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": inventory_crop.id,
                "event_type": "return_in",
                "quantity": "2.00",
                "notes": "non-staff boundary check",
            },
        )
        self.assertEqual(inventory_response.status_code, 403)
        self.assertEqual(InventoryLedger.objects.count(), 0)

        sales_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                "total_cash": "10.00",
                "total_card": "5.00",
                "notes": "non-staff boundary check",
            },
        )
        self.assertEqual(sales_response.status_code, 403)
        self.assertEqual(QuickSalesEntry.objects.count(), 0)

    def test_staff_role_mutation_boundary_remains_green_after_non_staff_denial(self):
        planting, channel = self._bootstrap_core_workflow_records()
        inventory_crop = planting.crop
        self._authenticate_non_staff_user()
        self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": inventory_crop.id,
                "event_type": "return_in",
                "quantity": "2.00",
                "notes": "non-staff boundary check",
            },
        )
        self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                "total_cash": "10.00",
                "total_card": "5.00",
                "notes": "non-staff boundary check",
            },
        )
        self.client.logout()
        self._authenticate_operator()

        status_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(status_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "planted")

        inventory_response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": inventory_crop.id,
                "event_type": "return_in",
                "quantity": "3.00",
                "notes": "staff boundary check",
            },
        )
        self.assertEqual(inventory_response.status_code, 302)
        self.assertEqual(InventoryLedger.objects.count(), 1)

        sales_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                "total_cash": "20.00",
                "total_card": "10.00",
                "notes": "staff boundary check",
            },
        )
        self.assertEqual(sales_response.status_code, 302)
        self.assertEqual(QuickSalesEntry.objects.count(), 1)

    def test_malformed_critical_mutation_payloads_do_not_raise_server_errors(self):
        planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        status_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "definitely-not-a-valid-status"},
        )
        self.assertEqual(status_response.status_code, 400)

        sales_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "bad-date",
                "total_cash": "not-a-decimal",
                "total_card": "not-a-decimal",
                "notes": "malformed payload gate check",
            },
        )
        self.assertEqual(sales_response.status_code, 400)
        self.assertEqual(InventoryLedger.objects.count(), 0)
        self.assertEqual(QuickSalesEntry.objects.count(), 0)

    def test_inventory_add_rejects_malformed_quantity_payload_with_400(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": planting.crop_id,
                "event_type": "return_in",
                "quantity": "not-a-number",
                "notes": "malformed inventory quantity gate check",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(InventoryLedger.objects.count(), 0)

    def test_inventory_add_rejects_missing_crop_and_invalid_event_type_payloads_with_400(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        missing_crop_response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "event_type": "return_in",
                "quantity": "1.00",
                "notes": "missing crop malformed payload gate check",
            },
        )
        self.assertEqual(missing_crop_response.status_code, 400)

        invalid_event_type_response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": planting.crop_id,
                "event_type": "definitely-not-a-valid-event",
                "quantity": "1.00",
                "notes": "invalid event type malformed payload gate check",
            },
        )
        self.assertEqual(invalid_event_type_response.status_code, 400)
        self.assertEqual(InventoryLedger.objects.count(), 0)

    def test_planting_status_rejects_missing_status_payload_with_400(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {},
        )
        self.assertEqual(response.status_code, 400)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "planned")

    def test_sales_market_entry_rejects_missing_or_unknown_channel_payloads(self):
        _planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        missing_channel_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "sale_date": "2026-06-14",
                "total_cash": "10.00",
                "total_card": "5.00",
            },
        )
        self.assertEqual(missing_channel_response.status_code, 400)

        unknown_channel_response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id + 999,
                "sale_date": "2026-06-14",
                "total_cash": "10.00",
                "total_card": "5.00",
            },
        )
        self.assertEqual(unknown_channel_response.status_code, 400)
        self.assertEqual(QuickSalesEntry.objects.count(), 0)

    def test_sales_market_entry_rejects_missing_sale_date_payload(self):
        _planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "total_cash": "10.00",
                "total_card": "5.00",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(QuickSalesEntry.objects.count(), 0)

    def test_sales_market_entry_quick_rejects_invalid_decimal_totals_with_400(self):
        _planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "quick",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                "total_cash": "cash??",
                "total_card": "card??",
                "notes": "invalid decimal totals malformed payload gate check",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(QuickSalesEntry.objects.count(), 0)

    def test_sales_detailed_entry_tolerates_invalid_decimal_payload_without_500(self):
        planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()
        product = CropSalesFormat.objects.create(
            crop=planting.crop,
            product_name="Carrot Bunch",
            sale_price=Decimal("3.50"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            is_active=True,
        )

        response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "detailed",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                f"sold_{product.id}": "not-a-decimal",
                f"price_{product.id}": "also-not-a-decimal",
                f"brought_{product.id}": "still-not-a-decimal",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SalesEvent.objects.count(), 0)

    def test_sales_detailed_entry_falls_back_to_default_price_when_override_is_invalid(self):
        planting, channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()
        product = CropSalesFormat.objects.create(
            crop=planting.crop,
            product_name="Carrot Bundle",
            sale_price=Decimal("4.00"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            is_active=True,
        )

        response = self.client.post(
            reverse("sales:market_entry"),
            {
                "mode": "detailed",
                "channel_id": channel.id,
                "sale_date": "2026-06-14",
                f"sold_{product.id}": "2",
                f"price_{product.id}": "invalid-price",
                f"brought_{product.id}": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SalesEvent.objects.count(), 1)
        event = SalesEvent.objects.get()
        self.assertEqual(event.product_id, product.id)
        self.assertEqual(event.actual_price, Decimal("4.00"))
        self.assertEqual(event.actual_revenue, Decimal("8.00"))
        self.assertEqual(event.returned_quantity, Decimal("1.00"))

    def test_planting_status_replay_idempotence_preserves_initial_actual_plant_date(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        first_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(first_response.status_code, 302)
        planting.refresh_from_db()
        first_actual_plant_date = planting.actual_plant_date
        self.assertIsNotNone(first_actual_plant_date)

        second_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(second_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.actual_plant_date, first_actual_plant_date)

    def test_planting_status_replay_preserves_first_harvest_and_completion_dates(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        planted_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(planted_response.status_code, 302)

        harvesting_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "harvesting"},
        )
        self.assertEqual(harvesting_response.status_code, 302)
        planting.refresh_from_db()
        first_harvest_date = planting.actual_first_harvest_date
        self.assertIsNotNone(first_harvest_date)

        harvesting_replay_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "harvesting"},
        )
        self.assertEqual(harvesting_replay_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.actual_first_harvest_date, first_harvest_date)

        complete_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "complete"},
        )
        self.assertEqual(complete_response.status_code, 302)
        planting.refresh_from_db()
        completion_date = planting.actual_last_harvest_date
        self.assertIsNotNone(completion_date)

        complete_replay_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "complete"},
        )
        self.assertEqual(complete_replay_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.actual_last_harvest_date, completion_date)

    def test_planting_status_rejects_illegal_transition_jumps(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        illegal_jump_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "complete"},
        )
        self.assertEqual(illegal_jump_response.status_code, 400)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "planned")

    def test_planting_status_accepts_expected_transition_sequence(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        for next_status in ("planted", "growing", "harvesting", "complete"):
            response = self.client.post(
                reverse("planning:planting_status", kwargs={"pk": planting.pk}),
                {"status": next_status},
            )
            self.assertEqual(response.status_code, 302)
            planting.refresh_from_db()
            self.assertEqual(planting.status, next_status)

    def test_planting_status_revised_can_return_to_planned(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        revised_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "revised"},
        )
        self.assertEqual(revised_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "revised")

        planned_response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planned"},
        )
        self.assertEqual(planned_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "planned")

    def test_planting_status_failed_cannot_jump_back_to_harvesting(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        fail_response = self._post_planting_status(planting, "failed")
        self.assertEqual(fail_response.status_code, 302)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "failed")

        invalid_recovery_response = self._post_planting_status(planting, "harvesting")
        self.assertEqual(invalid_recovery_response.status_code, 400)
        planting.refresh_from_db()
        self.assertEqual(planting.status, "failed")

    def test_planting_status_replay_matrix_keeps_dates_stable_across_legal_transition_paths(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        expected_dates = {
            "actual_plant_date": None,
            "actual_first_harvest_date": None,
            "actual_last_harvest_date": None,
        }
        transition_steps = [
            ("planned", None),
            ("seeded", None),
            ("seeded", None),
            ("planted", None),
            ("planted", None),
            ("growing", None),
            ("growing", None),
            ("harvesting", "actual_first_harvest_date"),
            ("harvesting", None),
            ("complete", "actual_last_harvest_date"),
            ("complete", None),
            ("revised", None),
            ("revised", None),
            ("planned", None),
            ("planned", None),
            ("skipped", None),
            ("skipped", None),
            ("revised", None),
            ("planned", None),
            ("failed", None),
            ("failed", None),
            ("revised", None),
            ("planned", None),
        ]

        for next_status, date_field_to_set in transition_steps:
            response = self._post_planting_status(planting, next_status)
            self.assertEqual(response.status_code, 302)
            planting.refresh_from_db()
            self.assertEqual(planting.status, next_status)
            if date_field_to_set and expected_dates[date_field_to_set] is None:
                expected_dates[date_field_to_set] = getattr(planting, date_field_to_set)
                self.assertIsNotNone(expected_dates[date_field_to_set])
            for field_name, expected_value in expected_dates.items():
                self.assertEqual(getattr(planting, field_name), expected_value)

        self.assertIsNone(planting.actual_plant_date)
        self.assertIsNotNone(planting.actual_first_harvest_date)
        self.assertIsNotNone(planting.actual_last_harvest_date)

    def test_planting_status_revised_return_path_can_reenter_full_legal_sequence_without_date_drift(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()

        first_leg = ("planted", "harvesting", "complete", "revised", "planned")
        for next_status in first_leg:
            response = self._post_planting_status(planting, next_status)
            self.assertEqual(response.status_code, 302)

        planting.refresh_from_db()
        original_dates = (
            planting.actual_plant_date,
            planting.actual_first_harvest_date,
            planting.actual_last_harvest_date,
        )
        self.assertTrue(all(original_dates))
        self.assertEqual(planting.status, "planned")

        replay_leg = ("planted", "growing", "harvesting", "complete")
        for next_status in replay_leg:
            response = self._post_planting_status(planting, next_status)
            self.assertEqual(response.status_code, 302)

        planting.refresh_from_db()
        self.assertEqual(planting.status, "complete")
        self.assertEqual(
            (
                planting.actual_plant_date,
                planting.actual_first_harvest_date,
                planting.actual_last_harvest_date,
            ),
            original_dates,
        )

    def test_inventory_write_replay_keeps_running_balance_sequence_deterministic(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        self._authenticate_operator()
        crop_id = planting.crop_id

        self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": crop_id,
                "event_type": "return_in",
                "quantity": "10.00",
                "notes": "deterministic replay baseline",
            },
        )
        self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": crop_id,
                "event_type": "sale_out",
                "quantity": "3.00",
                "notes": "deterministic replay drawdown",
            },
        )
        self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": crop_id,
                "event_type": "sale_out",
                "quantity": "3.00",
                "notes": "deterministic replay drawdown",
            },
        )

        entries = list(InventoryLedger.objects.filter(crop_id=crop_id).order_by("id"))
        self.assertEqual(len(entries), 3)
        self.assertEqual(str(entries[0].quantity), "10.00")
        self.assertEqual(str(entries[0].running_balance), "10.00")
        self.assertEqual(str(entries[1].quantity), "-3.00")
        self.assertEqual(str(entries[1].running_balance), "7.00")
        self.assertEqual(str(entries[2].quantity), "-3.00")
        self.assertEqual(str(entries[2].running_balance), "4.00")

    def test_mismatch_apply_replay_keeps_escalation_summary_stable_after_initial_write(self):
        fixture_dir = self.fixture_root / "mismatch"
        with TemporaryDirectory() as output_dir:
            first_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-escalation-apply-first.json",
            )
            second_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-escalation-apply-second.json",
            )
            third_summary = self._run_import(
                str(fixture_dir),
                Path(output_dir) / "summary-mismatch-escalation-apply-third.json",
            )

        self.assertGreater(first_summary["results"]["totals"]["created"], 0)
        self.assertEqual(second_summary["results"]["totals"]["created"], 0)
        self.assertEqual(third_summary["results"]["totals"]["created"], 0)
        self.assertEqual(
            second_summary["results"]["escalation_summary"],
            third_summary["results"]["escalation_summary"],
        )
        self.assertEqual(
            second_summary["results"]["failure_signatures"],
            third_summary["results"]["failure_signatures"],
        )
        self.assertEqual(second_summary["results"]["row_errors"], third_summary["results"]["row_errors"])

    def test_inventory_same_day_writes_keep_running_balance_order_deterministic(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        crop = planting.crop
        event_day = date(2026, 6, 15)

        first = InventoryLedger.objects.create(
            crop=crop,
            event_date=event_day,
            event_type="return_in",
            quantity=Decimal("5.00"),
            notes="same-day deterministic step 1",
        )
        second = InventoryLedger.objects.create(
            crop=crop,
            event_date=event_day,
            event_type="sale_out",
            quantity=Decimal("-2.00"),
            notes="same-day deterministic step 2",
        )
        third = InventoryLedger.objects.create(
            crop=crop,
            event_date=event_day,
            event_type="return_in",
            quantity=Decimal("1.00"),
            notes="same-day deterministic step 3",
        )

        entries = list(InventoryLedger.objects.filter(crop=crop).order_by("event_date", "created_at", "id"))
        self.assertEqual([entry.id for entry in entries], [first.id, second.id, third.id])
        self.assertEqual([str(entry.quantity) for entry in entries], ["5.00", "-2.00", "1.00"])
        self.assertEqual([str(entry.running_balance) for entry in entries], ["5.00", "3.00", "4.00"])

    def test_inventory_same_day_identical_timestamp_writes_keep_running_balance_order_deterministic(self):
        planting, _channel = self._bootstrap_core_workflow_records()
        crop = planting.crop
        event_day = date(2026, 6, 15)
        fixed_created_at = timezone.make_aware(datetime(2026, 6, 15, 12, 0, 0))

        with patch("django.utils.timezone.now", return_value=fixed_created_at):
            first = InventoryLedger.objects.create(
                crop=crop,
                event_date=event_day,
                event_type="return_in",
                quantity=Decimal("5.00"),
                notes="same-timestamp deterministic step 1",
            )
            second = InventoryLedger.objects.create(
                crop=crop,
                event_date=event_day,
                event_type="sale_out",
                quantity=Decimal("-2.00"),
                notes="same-timestamp deterministic step 2",
            )
            third = InventoryLedger.objects.create(
                crop=crop,
                event_date=event_day,
                event_type="return_in",
                quantity=Decimal("1.00"),
                notes="same-timestamp deterministic step 3",
            )

        entries = list(InventoryLedger.objects.filter(crop=crop).order_by("event_date", "created_at", "id"))
        self.assertEqual([entry.id for entry in entries], [first.id, second.id, third.id])
        self.assertEqual([entry.created_at for entry in entries], [fixed_created_at] * 3)
        self.assertEqual([str(entry.quantity) for entry in entries], ["5.00", "-2.00", "1.00"])
        self.assertEqual([str(entry.running_balance) for entry in entries], ["5.00", "3.00", "4.00"])


class ImportReferenceDataCommandTests(TestCase):
    def _write_csv(self, data_dir, name, lines):
        Path(data_dir, name).write_text("\n".join(lines), encoding="utf-8")

    def _write_reference_fixture(self, data_dir):
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
                "choose crop,Field,10,40,1.2,6,65,3,30,na,,,,,",
            ],
        )
        self._write_csv(
            data_dir,
            "sales_channels.csv",
            [
                "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week,is_csa,Priority",
                "Farm Stand,Saturday + Sunday,1,52,$500.00,false,1",
            ],
        )
        # Not consumed by import_reference_data today; included to prove extra files are ignored.
        self._write_csv(
            data_dir,
            "crop_sales_formats.csv",
            [
                "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                "Carrot,Carrot Bunch,3.50,bunch,1,CAR-BUN,true",
            ],
        )

    def test_dry_run_parses_reference_fixture_without_writing(self):
        with TemporaryDirectory() as data_dir:
            self._write_reference_fixture(data_dir)
            stdout = StringIO()

            call_command("import_reference_data", data_dir, "--dry-run", stdout=stdout)

            self.assertIn("DRY RUN", stdout.getvalue())
            self.assertEqual(Block.objects.count(), 0)
            self.assertEqual(CropInfo.objects.count(), 0)
            self.assertEqual(CropBySeason.objects.count(), 0)
            self.assertEqual(SalesChannel.objects.count(), 0)
            self.assertEqual(CropSalesFormat.objects.count(), 0)

    def test_import_writes_minimal_reference_records(self):
        with TemporaryDirectory() as data_dir:
            self._write_reference_fixture(data_dir)

            call_command("import_reference_data", data_dir)

            self.assertEqual(Block.objects.count(), 1)
            block = Block.objects.get(name="Field 1")
            self.assertEqual(block.block_type, "field")
            self.assertEqual(block.num_beds, 10)

            self.assertEqual(CropInfo.objects.count(), 1)
            crop = CropInfo.objects.get(name="Carrot")
            self.assertEqual(crop.harvest_unit, "pounds")
            self.assertEqual(crop.fresh_or_storage, "fresh")

            self.assertEqual(CropBySeason.objects.count(), 1)
            season = CropBySeason.objects.get(crop=crop, block_type="field")
            self.assertEqual(season.dtm_days, 65)
            self.assertEqual(str(season.total_yield_per_bedfoot), "1.20")

            self.assertEqual(SalesChannel.objects.count(), 1)
            channel = SalesChannel.objects.get(name="Farm Stand")
            self.assertEqual(channel.days_of_week, ["Saturday", "Sunday"])
            self.assertEqual(str(channel.weekly_target), "500.00")

    def test_reference_import_detects_header_after_variable_preamble(self):
        with TemporaryDirectory() as data_dir:
            self._write_csv(
                data_dir,
                "blocks.csv",
                [
                    "Farm Planning Export",
                    "Generated,2026-04-15",
                    "",
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed",
                    "Field 1,Field,10,3,100",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_info.csv",
                [
                    "Notes,Reference crop table",
                    "Crop,Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Units,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Size,Seeded Tray Size,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Carrot,Vegetables,Apiaceae,Fresh,0,pounds,1,,,,,0,0,,,1,0,",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_by_season.csv",
                [
                    "Instructions,Pick one row per crop profile",
                    "",
                    "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                    "Carrot,Field,10,40,1.2,6,65,3,30,na,,,,,",
                ],
            )
            self._write_csv(
                data_dir,
                "sales_channels.csv",
                [
                    "Title,Sales channels for season",
                    "Channel Name,Days of the Week,Start Week Num,End Week Num,$ Target per week,is_csa,Priority",
                    "Farm Stand,Saturday + Sunday,1,52,$500.00,false,1",
                ],
            )

            call_command("import_reference_data", data_dir)

            self.assertEqual(Block.objects.count(), 1)
            self.assertEqual(CropInfo.objects.count(), 1)
            self.assertEqual(CropBySeason.objects.count(), 1)
            self.assertEqual(SalesChannel.objects.count(), 1)

    def test_reference_import_applies_alias_headers(self):
        with TemporaryDirectory() as data_dir:
            self._write_csv(
                data_dir,
                "blocks.csv",
                [
                    "Block Name,Block Type,Number of Beds,Bed Width Feet,Bedfeet per Bed",
                    "Field 1,Field,10,3,100",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_info.csv",
                [
                    "Crop Name,Type,Botanical Family,Fresh/Storage,Storage Weeks,Harvest Units,Average Unit Wt,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate Units Per Hour,Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Size,Seeded Tray Size,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Carrot,Vegetables,Apiaceae,Fresh,0,pounds,1,,,,,0,0,,,1,0,",
                ],
            )
            self._write_csv(
                data_dir,
                "crop_by_season.csv",
                [
                    "Crop Name,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM,Rows Per Bed,DS Seed Rate Seeds Rowfoot,TP Inrow Spacing Ft,Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                    "Carrot,High Tunnel,10,40,1.2,6,65,3,30,na,,,,,",
                ],
            )
            self._write_csv(
                data_dir,
                "sales_channels.csv",
                [
                    "Channel,Days of Week,Start Week,End Week,Target per Week,is_csa,Priority",
                    "Farm Stand,Saturday + Sunday,1,52,$500.00,false,1",
                ],
            )

            call_command("import_reference_data", data_dir)

            season = CropBySeason.objects.get()
            self.assertEqual(season.block_type, "high_tunnel")
            self.assertEqual(SalesChannel.objects.get().days_of_week, ["Saturday", "Sunday"])


class GoogleSheetsStageA2ConnectorTests(TestCase):
    def test_extract_google_ids_from_urls(self):
        self.assertEqual(
            extract_drive_folder_id(
                "https://drive.google.com/drive/folders/1L_khaFUYinodAHg4r2_UelEp1_eA9QRJ?usp=drive_link"
            ),
            "1L_khaFUYinodAHg4r2_UelEp1_eA9QRJ",
        )
        self.assertEqual(
            extract_spreadsheet_id(
                "https://docs.google.com/spreadsheets/d/1abcDEFghiJKLmnOPqRSTuvwXYZ1234567890/edit#gid=0"
            ),
            "1abcDEFghiJKLmnOPqRSTuvwXYZ1234567890",
        )

    @patch("core.management.commands.pull_stage_a2_bundle.fetch_tab_rows")
    @patch("core.management.commands.pull_stage_a2_bundle.resolve_spreadsheet")
    @patch("core.management.commands.pull_stage_a2_bundle.build_google_service")
    def test_pull_stage_a2_bundle_writes_normalized_csv_and_manifest(
        self,
        build_google_service_mock,
        resolve_spreadsheet_mock,
        fetch_tab_rows_mock,
    ):
        build_google_service_mock.side_effect = [object(), object()]
        resolve_spreadsheet_mock.return_value = {
            "spreadsheet_id": "sheet-123",
            "spreadsheet_name": "Archive 2025",
            "modified_time": "2026-04-16T10:00:00Z",
        }
        fetch_tab_rows_mock.return_value = [
            ["Farm Archive Export"],
            ["Block Name", "Block Type", "Number of Beds", "Bed Width Feet", "Bedfeet per Bed"],
            ["Field 1", "Field", "10", "3", "100"],
        ]

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "live-config.json"
            output_dir = temp_path / "bundle"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "drive-folder-stage-a2",
                        "drive_folder_url": "https://drive.google.com/drive/folders/1L_khaFUYinodAHg4r2_UelEp1_eA9QRJ?usp=drive_link",
                        "tabs": [
                            {
                                "spreadsheet_name": "Archive 2025",
                                "worksheet_title": "Growing Space",
                                "output_path": "reference/blocks.csv",
                                "required_headers": [
                                    "Block",
                                    "Block Type",
                                    "# of Beds",
                                    "Bed Width (feet)",
                                    "Bedfeet per Bed",
                                ],
                                "aliases": {
                                    "block name": "Block",
                                    "number of beds": "# of Beds",
                                    "bed width feet": "Bed Width (feet)",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = StringIO()
            call_command(
                "pull_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
                stdout=stdout,
            )

            output_csv = output_dir / "reference" / "blocks.csv"
            self.assertTrue(output_csv.exists())
            self.assertEqual(
                output_csv.read_text(encoding="utf-8").splitlines(),
                [
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed",
                    "Field 1,Field,10,3,100",
                ],
            )

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["provider"], "google_sheets")
            self.assertEqual(manifest["drive_folder_id"], "1L_khaFUYinodAHg4r2_UelEp1_eA9QRJ")
            self.assertEqual(len(manifest["tabs"]), 1)
            self.assertEqual(manifest["tabs"][0]["spreadsheet_id"], "sheet-123")
            self.assertEqual(manifest["tabs"][0]["worksheet_title"], "Growing Space")
            self.assertEqual(manifest["tabs"][0]["output_path"], "reference/blocks.csv")
            self.assertEqual(manifest["tabs"][0]["strategy"], "required_header_set_scan")
            self.assertEqual(manifest["tabs"][0]["rows_written"], 1)
            self.assertIn("pulled Archive 2025:Growing Space -> reference/blocks.csv", stdout.getvalue())

    @patch("core.management.commands.pull_stage_a2_bundle.fetch_tab_rows")
    @patch("core.management.commands.pull_stage_a2_bundle.resolve_spreadsheet")
    @patch("core.management.commands.pull_stage_a2_bundle.build_google_service")
    def test_pull_stage_a2_bundle_can_project_live_reference_columns(
        self,
        build_google_service_mock,
        resolve_spreadsheet_mock,
        fetch_tab_rows_mock,
    ):
        build_google_service_mock.side_effect = [object(), object()]
        resolve_spreadsheet_mock.return_value = {
            "spreadsheet_id": "sheet-202",
            "spreadsheet_name": "Product Formats 2026",
            "modified_time": "2026-04-16T10:00:00Z",
        }
        fetch_tab_rows_mock.return_value = [
            ["Choose Formats To Sell Your Crops"],
            ["Format", "Product", "Sale price", "Sale Units", "Harvest Qty", "Product SKU"],
            ["Arugula - 1/3 lb", "Arugula", "$7.00", "1/3 lb", "0.33", "-1/3 lb"],
        ]

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "live-config.json"
            output_dir = temp_path / "bundle"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "drive-folder-stage-a2",
                        "drive_folder_url": "https://drive.google.com/drive/folders/1L_khaFUYinodAHg4r2_UelEp1_eA9QRJ?usp=drive_link",
                        "tabs": [
                            {
                                "spreadsheet_name": "Product Formats 2026",
                                "worksheet_title": "Farm Crop Formats",
                                "output_path": "reference/crop_sales_formats.csv",
                                "required_headers": [
                                    "Format",
                                    "Product",
                                    "Sale price",
                                    "Sale Units",
                                    "Harvest Qty",
                                    "Product SKU",
                                ],
                                "output_headers": [
                                    "Crop Name",
                                    "Product Name",
                                    "Sale Price",
                                    "Sale Unit",
                                    "Harvest Qty Per Sale Unit",
                                    "SKU",
                                    "Is Active",
                                ],
                                "column_map": {
                                    "Crop Name": "Product",
                                    "Product Name": "Format",
                                    "Sale Price": "Sale price",
                                    "Sale Unit": "Sale Units",
                                    "Harvest Qty Per Sale Unit": "Harvest Qty",
                                    "SKU": "Product SKU",
                                },
                                "default_values": {
                                    "Is Active": "true",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            call_command(
                "pull_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            self.assertEqual(
                (output_dir / "reference" / "crop_sales_formats.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                    "Arugula,Arugula - 1/3 lb,$7.00,1/3 lb,0.33,-1/3 lb,true",
                ],
            )

    @patch("core.management.commands.pull_stage_a2_bundle.fetch_tab_rows")
    @patch("core.management.commands.pull_stage_a2_bundle.resolve_spreadsheet")
    @patch("core.management.commands.pull_stage_a2_bundle.build_google_service")
    def test_pull_stage_a2_bundle_can_translate_crop_planner_rows(
        self,
        build_google_service_mock,
        resolve_spreadsheet_mock,
        fetch_tab_rows_mock,
    ):
        build_google_service_mock.side_effect = [object(), object()]
        resolve_spreadsheet_mock.return_value = {
            "spreadsheet_id": "sheet-402",
            "spreadsheet_name": "Crop Plan 2026",
            "modified_time": "2026-04-16T10:00:00Z",
        }
        fetch_tab_rows_mock.return_value = [
            ["Yellow Columns - Enter Your Information"],
            ["Crop // Variety", "Block", "Bed #", "Harvest Safety Factor", "Plan Field Year", "Plan Field Week", "Plan Bedft"],
            ["Arugula // Astro", "B1", "11", "1.3", "2026", "15", "100"],
        ]

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "live-config.json"
            output_dir = temp_path / "bundle"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "drive-folder-stage-a2",
                        "drive_folder_url": "https://drive.google.com/drive/folders/1L_khaFUYinodAHg4r2_UelEp1_eA9QRJ?usp=drive_link",
                        "tabs": [
                            {
                                "spreadsheet_name": "Crop Plan 2026",
                                "worksheet_title": "Crop Planner",
                                "output_path": "year_2026/plantings.csv",
                                "required_headers": [
                                    "Crop // Variety",
                                    "Block",
                                    "Bed #",
                                    "Plan Field Year",
                                    "Plan Field Week",
                                    "Plan Bedft",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Variety",
                                    "Block",
                                    "Bed Start",
                                    "Bed End",
                                    "Planned Plant Date",
                                    "Planned Bedfeet",
                                    "Status",
                                ],
                                "column_map": {
                                    "Crop": "Crop // Variety",
                                    "Variety": "Crop // Variety",
                                    "Block": "Block",
                                    "Bed Start": "Bed #",
                                    "Bed End": "Bed #",
                                    "Planned Bedfeet": "Plan Bedft",
                                },
                                "default_values": {
                                    "Status": "Planned",
                                },
                                "row_transforms": [
                                    {
                                        "type": "split",
                                        "source": "Crop",
                                        "delimiter": "//",
                                        "left_target": "Crop",
                                        "right_target": "Variety",
                                    },
                                    {
                                        "type": "copy",
                                        "source": "Bed Start",
                                        "targets": ["Bed End"],
                                    },
                                    {
                                        "type": "week_monday",
                                        "year_source": "Plan Field Year",
                                        "week_source": "Plan Field Week",
                                        "target": "Planned Plant Date",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            call_command(
                "pull_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            self.assertEqual(
                (output_dir / "year_2026" / "plantings.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop,Variety,Block,Bed Start,Bed End,Planned Plant Date,Planned Bedfeet,Status",
                    "Arugula,Astro,B1,11,11,2026-04-06,100,Planned",
                ],
            )

class StageA2BaselineBundleTests(TestCase):
    def _write_csv(self, parent, name, lines):
        Path(parent, name).write_text("\n".join(lines), encoding="utf-8")

    def test_snapshot_stage_a2_bundle_can_emit_baseline_live_rehearsal_bundle(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "source-tabs"
            output_dir = temp_path / "bundle"
            source_dir.mkdir(parents=True, exist_ok=True)

            self._write_csv(
                source_dir,
                "blocks.csv",
                [
                    "Define Your Farm",
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed,Bedfeet in Block",
                    "Field 1,Field,10,3,100,1000",
                ],
            )
            self._write_csv(
                source_dir,
                "crop_info.csv",
                [
                    "Crop Catalog",
                    "Crop,PRODUCT Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Unit,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Name,Seeded Tray Name,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Arugula,Greens,Brassicaceae,Fresh,0,bunch,0.25,40,Tote,Knife,30,0,0,,,1,0,12000",
                ],
            )
            self._write_csv(
                source_dir,
                "crop_sales_formats.csv",
                [
                    "Product Formats",
                    "Format,Product,Sale price,Sale Units,Harvest Qty,Product SKU",
                    "Arugula - 1/3 lb,Arugula,$7.00,1/3 lb,0.33,ARU-13",
                ],
            )
            self._write_csv(
                source_dir,
                "crop_planner.csv",
                [
                    "Yellow Columns - Enter Your Information",
                    "Crop // Variety,Block,Bed #,Harvest Safety Factor,Plan Field Year,Plan Field Week,Plan Bedft",
                    "Arugula // Astro,Field 1,11,1.3,2026,15,100",
                ],
            )

            config_path = temp_path / "snapshot-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "baseline-live-rehearsal-bundle",
                        "tabs": [
                            {
                                "source_csv": "source-tabs/blocks.csv",
                                "output_path": "reference/blocks.csv",
                                "required_headers": [
                                    "Block",
                                    "Block Type",
                                    "# of Beds",
                                    "Bed Width (feet)",
                                    "Bedfeet per Bed",
                                ],
                            },
                            {
                                "source_csv": "source-tabs/crop_info.csv",
                                "output_path": "reference/crop_info.csv",
                                "required_headers": [
                                    "Crop",
                                    "PRODUCT Type",
                                    "Botanical Family",
                                    "Fresh or Storage",
                                    "Storage Weeks",
                                    "Harvest Unit",
                                    "Average Unit Weight",
                                    "Units Per Bin",
                                    "Harvest Bin",
                                    "Harvest Tools",
                                    "Harvest Rate (units per hour)",
                                    "Nursery Weeks",
                                    "Weeks Until Pot Up",
                                    "Pot Up Tray Name",
                                    "Seeded Tray Name",
                                    "Seeds Per Cell",
                                    "Thinned Plants",
                                    "Seeds Per Ounce",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Type",
                                    "Botanical Family",
                                    "Fresh or Storage",
                                    "Storage Weeks",
                                    "Harvest Units",
                                    "Average Unit Weight",
                                    "Units Per Bin",
                                    "Harvest Bin",
                                    "Harvest Tools",
                                    "Harvest Rate (units per hour)",
                                    "Nursery Weeks",
                                    "Weeks Until Pot Up",
                                    "Pot Up Tray Size",
                                    "Seeded Tray Size",
                                    "Seeds Per Cell",
                                    "Thinned Plants",
                                    "Seeds Per Ounce",
                                ],
                                "column_map": {
                                    "Type": "PRODUCT Type",
                                    "Harvest Units": "Harvest Unit",
                                    "Pot Up Tray Size": "Pot Up Tray Name",
                                    "Seeded Tray Size": "Seeded Tray Name",
                                },
                            },
                            {
                                "source_csv": "source-tabs/crop_sales_formats.csv",
                                "output_path": "reference/crop_sales_formats.csv",
                                "required_headers": [
                                    "Format",
                                    "Product",
                                    "Sale price",
                                    "Sale Units",
                                    "Harvest Qty",
                                    "Product SKU",
                                ],
                                "output_headers": [
                                    "Crop Name",
                                    "Product Name",
                                    "Sale Price",
                                    "Sale Unit",
                                    "Harvest Qty Per Sale Unit",
                                    "SKU",
                                    "Is Active",
                                ],
                                "column_map": {
                                    "Crop Name": "Product",
                                    "Product Name": "Format",
                                    "Sale Price": "Sale price",
                                    "Sale Unit": "Sale Units",
                                    "Harvest Qty Per Sale Unit": "Harvest Qty",
                                    "SKU": "Product SKU",
                                },
                                "default_values": {"Is Active": "true"},
                            },
                            {
                                "source_csv": "source-tabs/crop_planner.csv",
                                "output_path": "year_2026/plantings.csv",
                                "required_headers": [
                                    "Crop // Variety",
                                    "Block",
                                    "Bed #",
                                    "Plan Field Year",
                                    "Plan Field Week",
                                    "Plan Bedft",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Variety",
                                    "Block",
                                    "Bed Start",
                                    "Bed End",
                                    "Planned Plant Date",
                                    "Planned Bedfeet",
                                    "Status",
                                ],
                                "column_map": {
                                    "Crop": "Crop // Variety",
                                    "Variety": "Crop // Variety",
                                    "Block": "Block",
                                    "Bed Start": "Bed #",
                                    "Bed End": "Bed #",
                                    "Planned Bedfeet": "Plan Bedft",
                                },
                                "default_values": {"Status": "Planned"},
                                "row_transforms": [
                                    {
                                        "type": "split",
                                        "source": "Crop",
                                        "delimiter": "//",
                                        "left_target": "Crop",
                                        "right_target": "Variety",
                                    },
                                    {
                                        "type": "copy",
                                        "source": "Bed Start",
                                        "targets": ["Bed End"],
                                    },
                                    {
                                        "type": "week_monday",
                                        "year_source": "Plan Field Year",
                                        "week_source": "Plan Field Week",
                                        "target": "Planned Plant Date",
                                    },
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            self.assertEqual(
                (output_dir / "reference" / "blocks.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed,Bedfeet in Block",
                    "Field 1,Field,10,3,100,1000",
                ],
            )
            self.assertEqual(
                (output_dir / "reference" / "crop_info.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop,Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Units,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Size,Seeded Tray Size,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Arugula,Greens,Brassicaceae,Fresh,0,bunch,0.25,40,Tote,Knife,30,0,0,,,1,0,12000",
                ],
            )
            self.assertEqual(
                (output_dir / "reference" / "crop_sales_formats.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop Name,Product Name,Sale Price,Sale Unit,Harvest Qty Per Sale Unit,SKU,Is Active",
                    "Arugula,Arugula - 1/3 lb,$7.00,1/3 lb,0.33,ARU-13,true",
                ],
            )
            self.assertEqual(
                (output_dir / "year_2026" / "plantings.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop,Variety,Block,Bed Start,Bed End,Planned Plant Date,Planned Bedfeet,Status",
                    "Arugula,Astro,Field 1,11,11,2026-04-06,100,Planned",
                ],
            )

    def test_baseline_bundle_plus_support_inputs_can_run_validate_only_import(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "source-tabs"
            bundle_dir = temp_path / "bundle"
            summary_path = temp_path / "baseline-validate-summary.json"
            source_dir.mkdir(parents=True, exist_ok=True)

            self._write_csv(
                source_dir,
                "blocks.csv",
                [
                    "Define Your Farm",
                    "Block,Block Type,# of Beds,Bed Width (feet),Bedfeet per Bed,Bedfeet in Block",
                    "Field 1,Field,10,3,100,1000",
                ],
            )
            self._write_csv(
                source_dir,
                "crop_info.csv",
                [
                    "Crop Catalog",
                    "Crop,PRODUCT Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Unit,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Name,Seeded Tray Name,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Arugula,Greens,Brassicaceae,Fresh,0,bunch,0.25,40,Tote,Knife,30,0,0,,,1,0,12000",
                ],
            )
            self._write_csv(
                source_dir,
                "crop_sales_formats.csv",
                [
                    "Product Formats",
                    "Format,Product,Sale price,Sale Units,Harvest Qty,Product SKU",
                    "Arugula - 1/3 lb,Arugula,$7.00,1/3 lb,0.33,ARU-13",
                ],
            )
            self._write_csv(
                source_dir,
                "crop_planner.csv",
                [
                    "Yellow Columns - Enter Your Information",
                    "Crop // Variety,Block,Bed #,Harvest Safety Factor,Plan Field Year,Plan Field Week,Plan Bedft",
                    "Arugula // Astro,Field 1,11,1.3,2026,15,100",
                ],
            )

            config_path = temp_path / "snapshot-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "baseline-live-rehearsal-bundle",
                        "tabs": [
                            {
                                "source_csv": "source-tabs/blocks.csv",
                                "output_path": "reference/blocks.csv",
                                "required_headers": [
                                    "Block",
                                    "Block Type",
                                    "# of Beds",
                                    "Bed Width (feet)",
                                    "Bedfeet per Bed",
                                ],
                            },
                            {
                                "source_csv": "source-tabs/crop_info.csv",
                                "output_path": "reference/crop_info.csv",
                                "required_headers": [
                                    "Crop",
                                    "PRODUCT Type",
                                    "Botanical Family",
                                    "Fresh or Storage",
                                    "Storage Weeks",
                                    "Harvest Unit",
                                    "Average Unit Weight",
                                    "Units Per Bin",
                                    "Harvest Bin",
                                    "Harvest Tools",
                                    "Harvest Rate (units per hour)",
                                    "Nursery Weeks",
                                    "Weeks Until Pot Up",
                                    "Pot Up Tray Name",
                                    "Seeded Tray Name",
                                    "Seeds Per Cell",
                                    "Thinned Plants",
                                    "Seeds Per Ounce",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Type",
                                    "Botanical Family",
                                    "Fresh or Storage",
                                    "Storage Weeks",
                                    "Harvest Units",
                                    "Average Unit Weight",
                                    "Units Per Bin",
                                    "Harvest Bin",
                                    "Harvest Tools",
                                    "Harvest Rate (units per hour)",
                                    "Nursery Weeks",
                                    "Weeks Until Pot Up",
                                    "Pot Up Tray Size",
                                    "Seeded Tray Size",
                                    "Seeds Per Cell",
                                    "Thinned Plants",
                                    "Seeds Per Ounce",
                                ],
                                "column_map": {
                                    "Type": "PRODUCT Type",
                                    "Harvest Units": "Harvest Unit",
                                    "Pot Up Tray Size": "Pot Up Tray Name",
                                    "Seeded Tray Size": "Seeded Tray Name",
                                },
                            },
                            {
                                "source_csv": "source-tabs/crop_sales_formats.csv",
                                "output_path": "reference/crop_sales_formats.csv",
                                "required_headers": [
                                    "Format",
                                    "Product",
                                    "Sale price",
                                    "Sale Units",
                                    "Harvest Qty",
                                    "Product SKU",
                                ],
                                "output_headers": [
                                    "Crop Name",
                                    "Product Name",
                                    "Sale Price",
                                    "Sale Unit",
                                    "Harvest Qty Per Sale Unit",
                                    "SKU",
                                    "Is Active",
                                ],
                                "column_map": {
                                    "Crop Name": "Product",
                                    "Product Name": "Format",
                                    "Sale Price": "Sale price",
                                    "Sale Unit": "Sale Units",
                                    "Harvest Qty Per Sale Unit": "Harvest Qty",
                                    "SKU": "Product SKU",
                                },
                                "default_values": {"Is Active": "true"},
                            },
                            {
                                "source_csv": "source-tabs/crop_planner.csv",
                                "output_path": "year_2026/plantings.csv",
                                "required_headers": [
                                    "Crop // Variety",
                                    "Block",
                                    "Bed #",
                                    "Plan Field Year",
                                    "Plan Field Week",
                                    "Plan Bedft",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Variety",
                                    "Block",
                                    "Bed Start",
                                    "Bed End",
                                    "Planned Plant Date",
                                    "Planned Bedfeet",
                                    "Status",
                                ],
                                "column_map": {
                                    "Crop": "Crop // Variety",
                                    "Variety": "Crop // Variety",
                                    "Block": "Block",
                                    "Bed Start": "Bed #",
                                    "Bed End": "Bed #",
                                    "Planned Bedfeet": "Plan Bedft",
                                },
                                "default_values": {"Status": "Planned"},
                                "row_transforms": [
                                    {
                                        "type": "split",
                                        "source": "Crop",
                                        "delimiter": "//",
                                        "left_target": "Crop",
                                        "right_target": "Variety",
                                    },
                                    {
                                        "type": "copy",
                                        "source": "Bed Start",
                                        "targets": ["Bed End"],
                                    },
                                    {
                                        "type": "week_monday",
                                        "year_source": "Plan Field Year",
                                        "week_source": "Plan Field Week",
                                        "target": "Planned Plant Date",
                                    },
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(bundle_dir),
            )
            self._write_csv(
                bundle_dir / "reference",
                "crop_by_season.csv",
                [
                    "Crop,Block Type,Field Week Start,Field Week End,Total Yield Per Bedfoot,Harvest Weeks,DTM Days To Maturity,Rows Per Bed,DS Seed Rate (seeds/ rowfoot),TP Inrow Spacing (ft),Seeder Settings,Trellis System,Mulch,Row Cover,Irrigation",
                    "Arugula,Field,10,40,1.2,6,45,3,30,na,,,,,",
                ],
            )
            year_dir = bundle_dir / "year_2026"
            year_dir.mkdir(parents=True, exist_ok=True)
            self._write_csv(
                year_dir,
                "planning_year.csv",
                [
                    "Year,Status,Overplant Factor",
                    "2026,planning,1.10",
                ],
            )

            call_command(
                "import_historical_data",
                str(bundle_dir),
                "--start-year",
                "2026",
                "--end-year",
                "2026",
                "--validate-only",
                "--summary-json",
                str(summary_path),
            )

            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["results"]["row_errors"], [])
            self.assertEqual(summary["results"]["totals"]["error"], 0)
            self.assertEqual(summary["results"]["models"]["PlanningYear"]["error"], 0)
            self.assertEqual(summary["results"]["models"]["Planting"]["error"], 0)
            self.assertGreaterEqual(summary["results"]["models"]["PlanningYear"]["skipped"], 1)
            self.assertGreaterEqual(summary["results"]["models"]["Planting"]["skipped"], 1)

    def test_snapshot_stage_a2_bundle_can_translate_crop_planner_with_unlabeled_first_column(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "source-tabs"
            output_dir = temp_path / "bundle"
            source_dir.mkdir(parents=True, exist_ok=True)

            self._write_csv(
                source_dir,
                "crop_planner_live_shape.csv",
                [
                    "",
                    "Yellow Columns - Enter Your Information",
                    ",Block,Bed #,HARVEST INFO,Location,Crop,Weekly Yield Per Bedfoot,Yield Units,Weeks To Maturity,Harvest Weeks,Storage Weeks,Availability,Planned First Harvest Year,Planned First Harvest Week,Planned Last Harvest Year,Planned Last Harvest Week,Last Storage Year,Last Storage Week,Yield,Harvest Need For Crop,Forecasted Total Harvest,Forecasted Weekly Harvest,Harvest To Allocate,FIELD PLAN,Harvest Safety Factor,Plan Field Year,Plan Field Week,Plan Bedft",
                    "Arugula // Astro,B1,11,,Field,Arugula,0.23,pounds,4,3,0,,2026,19,2026,21,2026,21,,119,53,18,-31,,1.3,2026,15,100",
                ],
            )

            config_path = temp_path / "snapshot-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "baseline-live-rehearsal-bundle",
                        "tabs": [
                            {
                                "source_csv": "source-tabs/crop_planner_live_shape.csv",
                                "output_path": "year_2026/plantings.csv",
                                "required_headers": [
                                    "Crop // Variety",
                                    "Block",
                                    "Bed #",
                                    "Plan Field Year",
                                    "Plan Field Week",
                                    "Plan Bedft",
                                ],
                                "header_row_index": 2,
                                "output_headers": [
                                    "Crop",
                                    "Variety",
                                    "Block",
                                    "Bed Start",
                                    "Bed End",
                                    "Planned Plant Date",
                                    "Planned Bedfeet",
                                    "Status",
                                ],
                                "column_map": {
                                    "Crop": 0,
                                    "Variety": 0,
                                    "Block": "Block",
                                    "Bed Start": "Bed #",
                                    "Bed End": "Bed #",
                                    "Planned Bedfeet": "Plan Bedft",
                                },
                                "default_values": {"Status": "Planned"},
                                "row_transforms": [
                                    {
                                        "type": "split",
                                        "source": "Crop",
                                        "delimiter": "//",
                                        "left_target": "Crop",
                                        "right_target": "Variety",
                                    },
                                    {
                                        "type": "copy",
                                        "source": "Bed Start",
                                        "targets": ["Bed End"],
                                    },
                                    {
                                        "type": "week_monday",
                                        "year_source": "Plan Field Year",
                                        "week_source": "Plan Field Week",
                                        "target": "Planned Plant Date",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            call_command(
                "snapshot_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            self.assertEqual(
                (output_dir / "year_2026" / "plantings.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop,Variety,Block,Bed Start,Bed End,Planned Plant Date,Planned Bedfeet,Status",
                    "Arugula,Astro,B1,11,11,2026-04-06,100,Planned",
                ],
            )

    @patch("core.management.commands.pull_stage_a2_bundle.fetch_tab_rows")
    @patch("core.management.commands.pull_stage_a2_bundle.resolve_spreadsheet")
    @patch("core.management.commands.pull_stage_a2_bundle.build_google_service")
    def test_pull_stage_a2_bundle_can_emit_baseline_live_rehearsal_bundle(
        self,
        build_google_service_mock,
        resolve_spreadsheet_mock,
        fetch_tab_rows_mock,
    ):
        build_google_service_mock.side_effect = [object(), object()]
        resolve_spreadsheet_mock.side_effect = [
            {
                "spreadsheet_id": "sheet-103",
                "spreadsheet_name": "Define Your Farm 2026",
                "modified_time": "2026-04-16T10:00:00Z",
            },
            {
                "spreadsheet_id": "sheet-201",
                "spreadsheet_name": "Crop List 2026",
                "modified_time": "2026-04-16T10:05:00Z",
            },
            {
                "spreadsheet_id": "sheet-202",
                "spreadsheet_name": "Product Formats 2026",
                "modified_time": "2026-04-16T10:10:00Z",
            },
            {
                "spreadsheet_id": "sheet-402",
                "spreadsheet_name": "Crop Plan 2026",
                "modified_time": "2026-04-16T10:15:00Z",
            },
        ]
        fetch_tab_rows_mock.side_effect = [
            [
                ["Define Your Farm"],
                ["Block", "Block Type", "# of Beds", "Bed Width (feet)", "Bedfeet per Bed", "Bedfeet in Block"],
                ["Field 1", "Field", "10", "3", "100", "1000"],
            ],
            [
                ["Crop Catalog"],
                ["Crop", "PRODUCT Type", "Botanical Family", "Fresh or Storage", "Storage Weeks", "Harvest Unit", "Average Unit Weight", "Units Per Bin", "Harvest Bin", "Harvest Tools", "Harvest Rate (units per hour)", "Nursery Weeks", "Weeks Until Pot Up", "Pot Up Tray Name", "Seeded Tray Name", "Seeds Per Cell", "Thinned Plants", "Seeds Per Ounce"],
                ["Arugula", "Greens", "Brassicaceae", "Fresh", "0", "bunch", "0.25", "40", "Tote", "Knife", "30", "0", "0", "", "", "1", "0", "12000"],
            ],
            [
                ["Product Formats"],
                ["Format", "Product", "Sale price", "Sale Units", "Harvest Qty", "Product SKU"],
                ["Arugula - 1/3 lb", "Arugula", "$7.00", "1/3 lb", "0.33", "ARU-13"],
            ],
            [
                ["Yellow Columns - Enter Your Information"],
                ["Crop // Variety", "Block", "Bed #", "Harvest Safety Factor", "Plan Field Year", "Plan Field Week", "Plan Bedft"],
                ["Arugula // Astro", "Field 1", "11", "1.3", "2026", "15", "100"],
            ],
        ]

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "live-config.json"
            output_dir = temp_path / "bundle"
            config_path.write_text(
                json.dumps(
                    {
                        "source_id": "baseline-live-rehearsal-bundle",
                        "drive_folder_url": "https://drive.google.com/drive/folders/1L_khaFUYinodAHg4r2_UelEp1_eA9QRJ?usp=drive_link",
                        "tabs": [
                            {
                                "spreadsheet_name": "Define Your Farm 2026",
                                "worksheet_title": "Define Field Blocks",
                                "output_path": "reference/blocks.csv",
                                "required_headers": [
                                    "Block",
                                    "Block Type",
                                    "# of Beds",
                                    "Bed Width (feet)",
                                    "Bedfeet per Bed",
                                ],
                            },
                            {
                                "spreadsheet_name": "Crop List 2026",
                                "worksheet_title": "Crop Info",
                                "output_path": "reference/crop_info.csv",
                                "required_headers": [
                                    "Crop",
                                    "PRODUCT Type",
                                    "Botanical Family",
                                    "Fresh or Storage",
                                    "Storage Weeks",
                                    "Harvest Unit",
                                    "Average Unit Weight",
                                    "Units Per Bin",
                                    "Harvest Bin",
                                    "Harvest Tools",
                                    "Harvest Rate (units per hour)",
                                    "Nursery Weeks",
                                    "Weeks Until Pot Up",
                                    "Pot Up Tray Name",
                                    "Seeded Tray Name",
                                    "Seeds Per Cell",
                                    "Thinned Plants",
                                    "Seeds Per Ounce",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Type",
                                    "Botanical Family",
                                    "Fresh or Storage",
                                    "Storage Weeks",
                                    "Harvest Units",
                                    "Average Unit Weight",
                                    "Units Per Bin",
                                    "Harvest Bin",
                                    "Harvest Tools",
                                    "Harvest Rate (units per hour)",
                                    "Nursery Weeks",
                                    "Weeks Until Pot Up",
                                    "Pot Up Tray Size",
                                    "Seeded Tray Size",
                                    "Seeds Per Cell",
                                    "Thinned Plants",
                                    "Seeds Per Ounce",
                                ],
                                "column_map": {
                                    "Type": "PRODUCT Type",
                                    "Harvest Units": "Harvest Unit",
                                    "Pot Up Tray Size": "Pot Up Tray Name",
                                    "Seeded Tray Size": "Seeded Tray Name",
                                },
                            },
                            {
                                "spreadsheet_name": "Product Formats 2026",
                                "worksheet_title": "Farm Crop Formats",
                                "output_path": "reference/crop_sales_formats.csv",
                                "required_headers": [
                                    "Format",
                                    "Product",
                                    "Sale price",
                                    "Sale Units",
                                    "Harvest Qty",
                                    "Product SKU",
                                ],
                                "output_headers": [
                                    "Crop Name",
                                    "Product Name",
                                    "Sale Price",
                                    "Sale Unit",
                                    "Harvest Qty Per Sale Unit",
                                    "SKU",
                                    "Is Active",
                                ],
                                "column_map": {
                                    "Crop Name": "Product",
                                    "Product Name": "Format",
                                    "Sale Price": "Sale price",
                                    "Sale Unit": "Sale Units",
                                    "Harvest Qty Per Sale Unit": "Harvest Qty",
                                    "SKU": "Product SKU",
                                },
                                "default_values": {"Is Active": "true"},
                            },
                            {
                                "spreadsheet_name": "Crop Plan 2026",
                                "worksheet_title": "Crop Planner",
                                "output_path": "year_2026/plantings.csv",
                                "required_headers": [
                                    "Crop // Variety",
                                    "Block",
                                    "Bed #",
                                    "Plan Field Year",
                                    "Plan Field Week",
                                    "Plan Bedft",
                                ],
                                "output_headers": [
                                    "Crop",
                                    "Variety",
                                    "Block",
                                    "Bed Start",
                                    "Bed End",
                                    "Planned Plant Date",
                                    "Planned Bedfeet",
                                    "Status",
                                ],
                                "column_map": {
                                    "Crop": "Crop // Variety",
                                    "Variety": "Crop // Variety",
                                    "Block": "Block",
                                    "Bed Start": "Bed #",
                                    "Bed End": "Bed #",
                                    "Planned Bedfeet": "Plan Bedft",
                                },
                                "default_values": {"Status": "Planned"},
                                "row_transforms": [
                                    {
                                        "type": "split",
                                        "source": "Crop",
                                        "delimiter": "//",
                                        "left_target": "Crop",
                                        "right_target": "Variety",
                                    },
                                    {
                                        "type": "copy",
                                        "source": "Bed Start",
                                        "targets": ["Bed End"],
                                    },
                                    {
                                        "type": "week_monday",
                                        "year_source": "Plan Field Year",
                                        "week_source": "Plan Field Week",
                                        "target": "Planned Plant Date",
                                    },
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            call_command(
                "pull_stage_a2_bundle",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_id"], "baseline-live-rehearsal-bundle")
            self.assertEqual(
                [tab["output_path"] for tab in manifest["tabs"]],
                [
                    "reference/blocks.csv",
                    "reference/crop_info.csv",
                    "reference/crop_sales_formats.csv",
                    "year_2026/plantings.csv",
                ],
            )
            self.assertEqual(
                (output_dir / "reference" / "crop_info.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop,Type,Botanical Family,Fresh or Storage,Storage Weeks,Harvest Units,Average Unit Weight,Units Per Bin,Harvest Bin,Harvest Tools,Harvest Rate (units per hour),Nursery Weeks,Weeks Until Pot Up,Pot Up Tray Size,Seeded Tray Size,Seeds Per Cell,Thinned Plants,Seeds Per Ounce",
                    "Arugula,Greens,Brassicaceae,Fresh,0,bunch,0.25,40,Tote,Knife,30,0,0,,,1,0,12000",
                ],
            )
            self.assertEqual(
                (output_dir / "year_2026" / "plantings.csv").read_text(encoding="utf-8").splitlines(),
                [
                    "Crop,Variety,Block,Bed Start,Bed End,Planned Plant Date,Planned Bedfeet,Status",
                    "Arugula,Astro,Field 1,11,11,2026-04-06,100,Planned",
                ],
            )
