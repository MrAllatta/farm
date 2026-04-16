from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from planning.models import Planting, PlanningYear
from reference.models import Block, CropBySeason, CropInfo, CropSalesFormat, SalesChannel
from sales.models import SalesEvent


class PlanningSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
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
            fresh_or_storage="fresh",
            storage_weeks=0,
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
            total_yield_per_bedfoot="1.20",
            harvest_weeks=6,
            dtm_days=65,
            rows_per_bed=3,
        )
        planting_date = date(2026, 4, 1)
        first_harvest = planting_date + timedelta(days=crop_season.dtm_days)
        last_harvest = first_harvest + timedelta(weeks=crop_season.harvest_weeks - 1)
        Planting.objects.create(
            planning_year=planning_year,
            crop=crop,
            crop_season=crop_season,
            block=block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=planting_date,
            planned_first_harvest_date=first_harvest,
            planned_last_harvest_date=last_harvest,
            planned_total_yield="120.00",
            status="planned",
        )

    def test_planning_matrix_route_renders_with_existing_plantings(self):
        response = self.client.get("/planning/")
        self.assertLess(response.status_code, 500)
        self.assertNotEqual(response.status_code, 404)


class PlantingLifecycleTransitionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.planning_year = PlanningYear.objects.create(year=2026, status="active")
        cls.block = Block.objects.create(
            name="Field 1",
            block_type="field",
            num_beds=10,
            bed_width_feet="3.0",
            bedfeet_per_bed=100,
        )
        cls.crop = CropInfo.objects.create(
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
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot="1.20",
            harvest_weeks=6,
            dtm_days=65,
            rows_per_bed=3,
        )

    def _create_planting(self, status="planned"):
        planting_date = date(2026, 4, 1)
        first_harvest = planting_date + timedelta(days=self.crop_season.dtm_days)
        last_harvest = first_harvest + timedelta(weeks=self.crop_season.harvest_weeks - 1)
        return Planting.objects.create(
            planning_year=self.planning_year,
            crop=self.crop,
            crop_season=self.crop_season,
            block=self.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=planting_date,
            planned_first_harvest_date=first_harvest,
            planned_last_harvest_date=last_harvest,
            planned_total_yield="120.00",
            status=status,
        )

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "planner-status", "planner-status@example.com", "x", is_staff=True
        )
        self.client.force_login(self.user)

    def test_planned_to_planted_sets_actual_plant_date_once(self):
        planting = self._create_planting(status="planned")

        response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(response.status_code, 302)

        planting.refresh_from_db()
        self.assertEqual(planting.status, "planted")
        self.assertEqual(planting.actual_plant_date, date.today())

        first_set_date = planting.actual_plant_date
        self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        planting.refresh_from_db()
        self.assertEqual(planting.actual_plant_date, first_set_date)

    def test_growing_to_harvesting_sets_actual_first_harvest_date(self):
        planting = self._create_planting(status="growing")
        self.assertIsNone(planting.actual_first_harvest_date)

        response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "harvesting"},
        )
        self.assertEqual(response.status_code, 302)

        planting.refresh_from_db()
        self.assertEqual(planting.status, "harvesting")
        self.assertEqual(planting.actual_first_harvest_date, date.today())

    def test_any_to_complete_sets_actual_last_harvest_date(self):
        planting = self._create_planting(status="harvesting")
        self.assertIsNone(planting.actual_last_harvest_date)

        response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "complete"},
        )
        self.assertEqual(response.status_code, 302)

        planting.refresh_from_db()
        self.assertEqual(planting.status, "complete")
        self.assertEqual(planting.actual_last_harvest_date, date.today())

    def test_invalid_transition_status_returns_400_and_keeps_state(self):
        planting = self._create_planting(status="planned")

        response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "not-a-real-status"},
        )
        self.assertEqual(response.status_code, 400)

        planting.refresh_from_db()
        self.assertEqual(planting.status, "planned")
        self.assertIsNone(planting.actual_plant_date)


class SalesPlanViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.planning_year = PlanningYear.objects.create(year=2026, status="active")
        cls.channel = SalesChannel.objects.create(
            name="Farm Stand",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target="500.00",
            is_csa=False,
            allocation_priority=1,
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
        cls.crop_season = CropBySeason.objects.create(
            crop=crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot="1.20",
            harvest_weeks=6,
            dtm_days=65,
            rows_per_bed=3,
        )
        cls.product = CropSalesFormat.objects.create(
            crop=crop,
            product_name="Carrot Bunch",
            sale_price="3.50",
            sale_unit="bunch",
            harvest_qty_per_sale_unit="1.00",
            is_active=True,
        )
        winter_crop = CropInfo.objects.create(
            name="Spinach",
            crop_type="Vegetables",
            botanical_family="Amaranthaceae",
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
        CropBySeason.objects.create(
            crop=winter_crop,
            block_type="field",
            field_week_start=48,
            field_week_end=6,
            total_yield_per_bedfoot="0.80",
            harvest_weeks=8,
            dtm_days=45,
            rows_per_bed=4,
        )
        cls.winter_product = CropSalesFormat.objects.create(
            crop=winter_crop,
            product_name="Spinach Bag",
            sale_price="4.00",
            sale_unit="bag",
            harvest_qty_per_sale_unit="1.00",
            is_active=True,
        )
        no_profile_crop = CropInfo.objects.create(
            name="Mystery Herb",
            crop_type="Herbs",
            botanical_family="Lamiaceae",
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
        cls.no_profile_product = CropSalesFormat.objects.create(
            crop=no_profile_crop,
            product_name="Mystery Herb Bunch",
            sale_price="2.50",
            sale_unit="bunch",
            harvest_qty_per_sale_unit="1.00",
            is_active=True,
        )

    def test_sales_plan_route_renders(self):
        response = self.client.get(reverse("planning:sales_plan"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sales Plan")
        self.assertContains(response, "sales-plan-cell--harvest")
        self.assertContains(response, "sales-plan-cell--off")

    def test_sales_plan_save_creates_plan_rows(self):
        user = get_user_model().objects.create_user(
            "planner", "planner@example.com", "x", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("planning:sales_plan"),
            {
                "action": "save",
                "channel": str(self.channel.id),
                f"qty_{self.product.id}_12": "9.5",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.planning_year,
                channel=self.channel,
                product=self.product,
            ).exists()
        )

    def test_sales_plan_week_cell_states_include_wraparound_and_unknown_profiles(self):
        response = self.client.get(reverse("planning:sales_plan"))
        self.assertEqual(response.status_code, 200)

        rows = response.context["product_rows"]
        carrot_row = next(r for r in rows if r["product"].id == self.product.id)
        spinach_row = next(r for r in rows if r["product"].id == self.winter_product.id)
        mystery_row = next(r for r in rows if r["product"].id == self.no_profile_product.id)

        carrot_week_12 = next(c for c in carrot_row["week_cells"] if c["week"] == 12)
        carrot_week_1 = next(c for c in carrot_row["week_cells"] if c["week"] == 1)
        spinach_week_2 = next(c for c in spinach_row["week_cells"] if c["week"] == 2)
        mystery_week_20 = next(c for c in mystery_row["week_cells"] if c["week"] == 20)

        self.assertEqual(carrot_week_12["window_state"], "harvest")
        self.assertEqual(carrot_week_1["window_state"], "off")
        self.assertEqual(spinach_week_2["window_state"], "harvest")
        self.assertEqual(mystery_week_20["window_state"], "unknown")
