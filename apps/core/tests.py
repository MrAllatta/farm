import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from planning.models import Planting, PlanningYear
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
            self.assertEqual(preflight_summary["results"]["totals"]["error"], 2)
            self.assertEqual(apply_summary["results"]["totals"]["error"], 2)
            self.assertEqual(CropBySeason.objects.count(), 0)

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
    PRIMARY_ROUTES = [
        ("core:dashboard", {}),
        ("planning:matrix", {}),
        ("operations:harvest_entry_current", {}),
        ("operations:inventory", {}),
        ("sales:market_entry", {}),
    ]

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
                self.assertContains(response, "<html", status_code=200)

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

    def test_unknown_route_returns_404(self):
        response = self.client.get("/definitely-not-a-real-route/")
        self.assertEqual(response.status_code, 404)
