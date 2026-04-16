from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from planning.models import HarvestEvent, Planting, PlanningYear
from reference.models import Block, BlockType, CropBySeason, CropInfo


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
