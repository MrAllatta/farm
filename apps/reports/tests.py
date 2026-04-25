from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from planning.models import HarvestEvent, Planting, PlanningYear
from operations.models import PackBatch, PackBatchComponent
from reference.models import Block, BlockType, CropBySeason, CropInfo, CropSalesFormat, SalesChannel
from reports.services.crop_maps import CropMapOccupancyService
from sales.models import QuickSalesEntry, SalesEvent


class HarvestListPrintViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        year = PlanningYear.objects.create(year=2026, status="active")
        crop = CropInfo.objects.create(
            name="Carrot",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            fresh_or_storage="fresh",
            harvest_unit="bunch",
            avg_unit_weight=Decimal("1.00"),
            units_per_bin=10,
            harvest_bin="yellow_tote",
            harvest_tools="knife",
        )
        crop_season = CropBySeason.objects.create(
            crop=crop,
            block_type=BlockType.FIELD,
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot=Decimal("1.20"),
            harvest_weeks=3,
            dtm_days=70,
            rows_per_bed=3,
        )
        block = Block.objects.create(
            name="Field 1",
            block_type=BlockType.FIELD,
            num_beds=10,
            bed_width_feet=Decimal("3.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        planting = Planting.objects.create(
            planning_year=year,
            crop=crop,
            crop_season=crop_season,
            variety="Nantes",
            block=block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=100,
            planned_plant_date=date(2026, 3, 1),
            planned_first_harvest_date=date(2026, 3, 17),
            planned_last_harvest_date=date(2026, 3, 31),
            planned_total_yield=Decimal("120.00"),
            status="growing",
        )
        HarvestEvent.objects.create(
            planting=planting,
            planned_date=date(2026, 3, 17),
            planned_quantity=Decimal("25.00"),
            planned_units="bunch",
        )
        skipped_planting = Planting.objects.create(
            planning_year=year,
            crop=crop,
            crop_season=crop_season,
            variety="Skip Me",
            block=block,
            bed_start=3,
            bed_end=4,
            planned_bedfeet=100,
            planned_plant_date=date(2026, 3, 1),
            planned_first_harvest_date=date(2026, 3, 17),
            planned_last_harvest_date=date(2026, 3, 31),
            planned_total_yield=Decimal("120.00"),
            status="skipped",
        )
        HarvestEvent.objects.create(
            planting=skipped_planting,
            planned_date=date(2026, 3, 17),
            planned_quantity=Decimal("30.00"),
            planned_units="bunch",
        )

    def test_harvest_list_print_view_builds_operator_context_from_week_events(self):
        response = self.client.get(reverse("reports:harvest_list_print", kwargs={"week": 12}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["view_title"], "Harvest List")
        self.assertTrue(response.context["show_week_navigation"])
        self.assertEqual(response.context["week_num"], 12)
        self.assertEqual(response.context["prev_week_num"], 11)
        self.assertEqual(response.context["next_week_num"], 13)
        self.assertEqual(response.context["total_items"], 1)
        self.assertEqual(response.context["total_bins"], 3)
        self.assertEqual(response.context["bin_totals"], [("yellow_tote", 3)])
        self.assertEqual(response.context["tools_needed"], ["knife"])

        item = response.context["items"][0]
        self.assertEqual(item["crop"], "Carrot")
        self.assertEqual(item["block"], "Field 1")
        self.assertEqual(item["beds"], "1-2")
        self.assertEqual(item["bins_needed"], 3)


class CropMapServiceAndViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = PlanningYear.objects.create(year=2026, status="active")
        cls.block = Block.objects.create(
            name="Field A",
            block_type=BlockType.FIELD,
            num_beds=4,
            bed_width_feet=Decimal("3.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        cls.crop = CropInfo.objects.create(
            name="Lettuce",
            crop_type="Greens",
            botanical_family="Asteraceae",
            fresh_or_storage="fresh",
            harvest_unit="head",
            avg_unit_weight=Decimal("1.00"),
        )
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.crop,
            block_type=BlockType.FIELD,
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot=Decimal("1.20"),
            harvest_weeks=2,
            dtm_days=21,
            rows_per_bed=3,
        )
        cls.active = Planting.objects.create(
            planning_year=cls.year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            variety="Summer",
            block=cls.block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=200,
            planned_plant_date=date(2026, 3, 2),  # week 10
            planned_first_harvest_date=date(2026, 3, 23),
            planned_last_harvest_date=date(2026, 4, 6),
            planned_total_yield=Decimal("240.00"),
            status="growing",
            succession_group="Lettuce-FieldA-2026",
        )
        cls.excluded = Planting.objects.create(
            planning_year=cls.year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            variety="Skip",
            block=cls.block,
            bed_start=3,
            bed_end=3,
            planned_bedfeet=100,
            planned_plant_date=date(2026, 3, 2),
            planned_first_harvest_date=date(2026, 3, 23),
            planned_last_harvest_date=date(2026, 4, 6),
            planned_total_yield=Decimal("120.00"),
            status="skipped",
        )

    def test_crop_map_service_excludes_skipped_and_builds_high_level_segments(self):
        service = CropMapOccupancyService(self.year)
        block_rows = service.get_high_level_block_map(week_num=13)
        row = block_rows[0]

        occupied = [s for s in row["segments"] if s["label"] != "fallow"]
        self.assertEqual(len(occupied), 1)
        self.assertEqual(occupied[0]["planting"].id, self.active.id)
        self.assertIn("P-2026-", occupied[0]["label"])
        self.assertEqual(int(row["utilization_pct"]), 50)

    def test_crop_map_service_groups_successions(self):
        service = CropMapOccupancyService(self.year)
        rows = service.get_successions_by_block()
        self.assertEqual(len(rows[0]["successions"]), 1)
        self.assertEqual(rows[0]["successions"][0]["succession_key"], "Lettuce-FieldA-2026")

    def test_crop_map_week_by_bed_route_renders(self):
        response = self.client.get(reverse("reports:crop_map_week_by_bed"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/crop_map_week_by_bed.html")
        self.assertIn("rows", response.context)
        self.assertEqual(response.context["visible_planting_count"], 1)
        self.assertContains(response, "Bed-by-bed occupancy")
        self.assertContains(response, "Weeks 27-52")
        self.assertContains(response, 'data-testid="fp-table-scroll"')
        self.assertContains(response, "fp-table-scroll")

    def test_crop_map_primary_route_renders(self):
        """High-level crop map (entry to block/bed week reports) must return 200."""
        response = self.client.get(reverse("reports:crop_map"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/crop_map.html")
        self.assertIn("field_maps", response.context)
        self.assertEqual(response.context["block_count"], 1)
        self.assertContains(response, "503 field map check")
        self.assertContains(response, "Planting units this week")

    def test_crop_map_week_by_block_route_renders(self):
        response = self.client.get(reverse("reports:crop_map_week_by_block"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/crop_map_week_by_block.html")
        self.assertIn("rows", response.context)
        self.assertEqual(response.context["visible_planting_count"], 1)
        self.assertContains(response, "Block-week occupancy")
        self.assertContains(response, "End of year")

    def test_crop_map_week_by_block_accepts_range_navigation(self):
        response = self.client.get(reverse("reports:crop_map_week_by_block"), {"start": 1, "end": 26})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["week_start"], 1)
        self.assertEqual(response.context["week_end"], 26)
        self.assertContains(response, "Showing weeks 1-26")
        self.assertContains(response, "Weeks 27-52")

    def test_crop_map_week_by_block_shows_planting_code_chips(self):
        self.active.refresh_from_db()
        response = self.client.get(reverse("reports:crop_map_week_by_block"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.active.planting_code)
        self.assertContains(response, "crop-map-chip")

    def test_planting_trace_renders_planting_code(self):
        self.active.refresh_from_db()
        response = self.client.get(
            reverse("reports:planting_trace", kwargs={"planting_id": self.active.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.active.planting_code)

    def test_crop_map_successions_route_renders(self):
        response = self.client.get(reverse("reports:crop_map_successions"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/crop_map_successions_by_block.html")
        self.assertIn("rows", response.context)


class AnalyzeViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = PlanningYear.objects.create(year=2026, status="active")
        cls.block = Block.objects.create(
            name="Field B",
            block_type=BlockType.FIELD,
            num_beds=6,
            bed_width_feet=Decimal("3.0"),
            bedfeet_per_bed=100,
            walk_route_order=2,
        )
        cls.crop = CropInfo.objects.create(
            name="Spinach",
            crop_type="Greens",
            botanical_family="Amaranthaceae",
            fresh_or_storage="fresh",
            harvest_unit="bag",
            avg_unit_weight=Decimal("1.00"),
        )
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.crop,
            block_type=BlockType.FIELD,
            field_week_start=8,
            field_week_end=42,
            total_yield_per_bedfoot=Decimal("1.50"),
            harvest_weeks=4,
            dtm_days=35,
            rows_per_bed=3,
        )
        cls.planting = Planting.objects.create(
            planning_year=cls.year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            variety="Space",
            block=cls.block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=150,
            actual_bedfeet=140,
            planned_plant_date=date(2026, 3, 2),
            actual_plant_date=date(2026, 3, 4),
            planned_first_harvest_date=date(2026, 4, 6),
            actual_first_harvest_date=date(2026, 4, 8),
            planned_last_harvest_date=date(2026, 4, 27),
            actual_last_harvest_date=date(2026, 4, 29),
            planned_total_yield=Decimal("225.00"),
            status="harvesting",
        )
        HarvestEvent.objects.create(
            planting=cls.planting,
            planned_date=date(2026, 4, 6),
            planned_quantity=Decimal("50.00"),
            planned_units="bag",
            actual_date=date(2026, 4, 6),
            actual_quantity=Decimal("48.00"),
            actual_units="bag",
            actual_hours=Decimal("2.5"),
        )
        cls.format = CropSalesFormat.objects.create(
            crop=cls.crop,
            product_name="Spinach Bag",
            sale_price=Decimal("5.00"),
            sale_unit="bag",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            is_active=True,
        )
        cls.channel = SalesChannel.objects.create(
            name="Saturday Market",
            days_of_week=["Sat"],
            start_week=10,
            end_week=40,
            weekly_target=Decimal("250.00"),
        )
        SalesEvent.objects.create(
            channel=cls.channel,
            sale_date=date(2026, 4, 11),
            planning_year=cls.year,
            product=cls.format,
            actual_quantity=Decimal("30.00"),
            actual_revenue=Decimal("150.00"),
            brought_quantity=Decimal("36.00"),
        )
        QuickSalesEntry.objects.create(
            channel=cls.channel,
            sale_date=date(2026, 4, 18),
            total_cash=Decimal("80.00"),
            total_card=Decimal("20.00"),
        )
        cls.mix_batch = PackBatch.objects.create(
            product=cls.format,
            packed_quantity=Decimal("40.00"),
            packed_unit="bag",
            pack_date=date(2026, 4, 11),
        )
        PackBatchComponent.objects.create(
            pack_batch=cls.mix_batch,
            source_crop=cls.crop,
            consumed_quantity=Decimal("35.00"),
            consumed_unit="bag",
            component_percent=Decimal("100.00"),
        )
        sales_event = SalesEvent.objects.filter(channel=cls.channel, sale_date=date(2026, 4, 11)).first()
        sales_event.pack_batch = cls.mix_batch
        sales_event.save(update_fields=["pack_batch"])

    def test_all_analyze_routes_render(self):
        routes = [
            ("reports:plan_vs_actual", "reports/plan_vs_actual.html"),
            ("reports:crop_performance", "reports/crop_performance.html"),
            ("reports:channel_performance", "reports/channel_performance.html"),
            ("reports:block_utilization", "reports/block_utilization.html"),
            ("reports:season_summary", "reports/season_summary.html"),
        ]

        for route_name, template_name in routes:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, template_name)
                self.assertIn("page_title", response.context)
                self.assertIn("analyze_links", response.context)
                self.assertIn("analyze_page", response.context)

    def test_channel_performance_exposes_pacing_and_products(self):
        response = self.client.get(reverse("reports:channel_performance"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_title"], "Channel Performance")
        self.assertEqual(response.context["channels"][0]["channel"].name, "Saturday Market")
        self.assertEqual(response.context["channels"][0]["top_products"][0]["product__product_name"], "Spinach Bag")
        self.assertIsNotNone(response.context["channels"][0]["pacing"])

    def test_season_summary_rolls_up_revenue_and_yield(self):
        response = self.client.get(reverse("reports:season_summary"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_title"], "Season Summary")
        self.assertEqual(response.context["total_revenue"], Decimal("250.00"))
        self.assertEqual(response.context["actual_yield_total"], Decimal("48.00"))
        self.assertEqual(response.context["mix_batches_count"], 1)
        self.assertEqual(response.context["mix_packed_qty"], Decimal("40.00"))
        self.assertEqual(response.context["mix_sold_qty"], Decimal("30.00"))
        self.assertEqual(response.context["mix_component_drawdown"], Decimal("35.00"))


class ReportTemplateParityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        PlanningYear.objects.create(year=2026, status="active")

    def test_pack_list_print_route_uses_dedicated_template(self):
        response = self.client.get(reverse("reports:pack_list_print", kwargs={"week": 12}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/pack_list_print.html")

    def test_weekly_schedule_print_route_uses_dedicated_template(self):
        response = self.client.get(reverse("reports:weekly_schedule_print", kwargs={"week": 12}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/weekly_schedule_print.html")

    def test_nursery_schedule_print_route_uses_dedicated_template(self):
        response = self.client.get(reverse("reports:nursery_schedule_print"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/nursery_schedule_print.html")

    def test_seed_order_route_uses_dedicated_template(self):
        response = self.client.get(reverse("reports:seed_order"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/seed_order.html")

    def test_crop_map_print_route_renders_with_custom_filters_loaded(self):
        response = self.client.get(reverse("reports:crop_map_print", kwargs={"week": 12}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reports/crop_map_print.html")


class SeedOrderReportGroupingTests(TestCase):
    def test_build_seed_order_rows_splits_by_variety(self):
        from isoweek import Week

        from planning.models import Planting
        from reports.services.seed_order_report import build_seed_order_rows

        year = PlanningYear.objects.create(year=3031, status="active")
        block = Block.objects.create(
            name="SO1",
            block_type=BlockType.FIELD,
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        crop = CropInfo.objects.create(
            name="SeedOrderCrop",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
            seeds_per_ounce=Decimal("1000"),
        )
        cs = CropBySeason.objects.create(
            crop=crop,
            block_type=BlockType.FIELD,
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
            ds_seed_rate=10,
        )
        from reference.models import Variety

        v1 = Variety.objects.create(crop=crop, name="Alpha")
        v2 = Variety.objects.create(crop=crop, name="Beta")
        mon = Week(3031, 10).monday()
        p1 = Planting.objects.create(
            planning_year=year,
            crop=crop,
            crop_season=cs,
            block=block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=mon,
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon,
            planned_total_yield=Decimal("100"),
            variety_obj=v1,
        )
        p2 = Planting.objects.create(
            planning_year=year,
            crop=crop,
            crop_season=cs,
            block=block,
            bed_start=2,
            bed_end=2,
            planned_bedfeet=50,
            planned_plant_date=mon,
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon,
            planned_total_yield=Decimal("50"),
            variety_obj=v2,
        )
        rows = build_seed_order_rows([p1, p2], 1.0)
        labels = sorted({r["variety_label"] for r in rows})
        self.assertEqual(labels, ["Alpha", "Beta"])
