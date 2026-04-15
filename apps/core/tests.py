import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import get_resolver, reverse

from operations.models import InventoryLedger
from planning.models import Planting, PlanningYear
from sales.models import QuickSalesEntry
from reference.models import Block, CropBySeason, CropInfo, CropSalesFormat, SalesChannel


class ImportHistoricalDataCommandTests(TestCase):
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
                "  FARM   STAND  ,2021-06-01,  CARROT   BUNCH  ,10,35,9,31.5,3.5,10,1,normalized lookup test",
            ],
        )
        self._write_csv(
            year_dir,
            "pack_allocations.csv",
            [
                "Planting ID,Harvest Date,Channel,Product,Pack Date,Quantity,Notes",
                "P1,, farm stand , carrot bunch ,2021-06-02,5,duplicate-safe lookup test",
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
        self.assertEqual(summary["schema_version"], "1.2")
        self.assertIn(summary["status"], {"ok", "failed"})
        self.assertIn("fatal_error", summary)
        self.assertEqual(
            set(summary["run"].keys()),
            {
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
            },
        )
        self.assertTrue(summary["run"]["run_id"])
        self.assertTrue(summary["run"]["started_at"])
        self.assertTrue(summary["run"]["finished_at"])
        self.assertEqual(summary["run"]["validate_only"], expected_validate_only)
        self.assertEqual(summary["run"]["dry_run"], expected_dry_run)
        if expected_atomic_apply is None:
            expected_atomic_apply = expected_validate_only or not expected_dry_run
        self.assertEqual(summary["run"]["atomic_apply"], expected_atomic_apply)
        self.assertEqual(
            set(summary["results"].keys()),
            {"models", "totals", "row_errors", "failure_signatures", "escalation_summary"},
        )
        self.assertEqual(set(summary["results"]["totals"].keys()), {"created", "updated", "skipped", "error"})
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
        expected_key_set = {"model", "row", "code", "field_path", "message"}
        expected_sorted_keys = ["code", "field_path", "message", "model", "row"]
        for item in row_errors:
            self.assertEqual(set(item.keys()), expected_key_set)
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
        expected_keys = {
            "signature",
            "count",
            "owner_area",
            "owner_team",
            "severity",
            "escalation_path",
            "recovery",
            "example",
        }
        example_keys = {"model", "field_path", "message"}
        for item in failure_signatures:
            self.assertEqual(set(item.keys()), expected_keys)
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
        expected_keys = {
            "owner_area",
            "owner_team",
            "severity",
            "escalation_path",
            "count",
            "signatures",
        }
        for item in escalation_summary:
            self.assertEqual(set(item.keys()), expected_keys)
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
                    },
                    {
                        "owner_area": "data-contracts",
                        "owner_team": "import-pipeline",
                        "severity": "medium",
                        "escalation_path": "ops-oncall -> data-contracts",
                        "count": 1,
                        "signatures": ["namespace_mismatch"],
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
        "reports": 7,
    }
    EXPECTED_PRIMARY_SMOKE_ROUTES_BY_NAMESPACE = {
        "core": 1,
        "reference": 1,
        "planning": 6,
        "operations": 4,
        "sales": 3,
        "reports": 7,
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
        self.assertIn(summary["schema_version"], {"1.1", "1.2"})
        self.assertIn(summary["status"], {"ok", "failed"})
        self.assertEqual(summary["run"]["validate_only"], expected_validate_only)
        self.assertEqual(summary["run"]["dry_run"], expected_dry_run)
        self.assertIn("atomic_apply", summary["run"])
        self.assertTrue({"models", "totals", "row_errors"} <= set(summary["results"].keys()))

    def test_critical_workflow_integration_path_persists_planning_operations_and_sales_records(self):
        planting, channel = self._bootstrap_core_workflow_records()

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
        self.assertEqual(validate_pairs, apply_pairs)
        self.assertEqual(validate_summary["results"]["totals"]["created"], 0)
        self.assertGreater(apply_summary["results"]["totals"]["created"], 0)

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
        for item in summary["results"]["failure_signatures"]:
            self.assertTrue(item["owner_area"])
            self.assertTrue(item["owner_team"])
            self.assertTrue(item["escalation_path"])
            self.assertTrue(item["recovery"])
            self.assertIn(item["severity"], {"high", "medium"})
