import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

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

    def _run_import(self, data_dir, summary_path, *extra_args):
        call_command("import_historical_data", data_dir, "--summary-json", str(summary_path), *extra_args)
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def _assert_summary_contract(self, summary, expected_validate_only, expected_dry_run=False):
        self.assertEqual(summary["schema_version"], "1.0")
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
                "verbose",
            },
        )
        self.assertTrue(summary["run"]["run_id"])
        self.assertTrue(summary["run"]["started_at"])
        self.assertTrue(summary["run"]["finished_at"])
        self.assertEqual(summary["run"]["validate_only"], expected_validate_only)
        self.assertEqual(summary["run"]["dry_run"], expected_dry_run)
        self.assertEqual(set(summary["results"].keys()), {"models", "totals"})
        self.assertEqual(set(summary["results"]["totals"].keys()), {"created", "updated", "skipped", "error"})

        model_totals = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
        for model_counts in summary["results"]["models"].values():
            self.assertEqual(set(model_counts.keys()), {"created", "updated", "skipped", "error"})
            for key in model_totals:
                model_totals[key] += model_counts[key]
        self.assertEqual(summary["results"]["totals"], model_totals)

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
