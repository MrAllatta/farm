from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.conf import settings
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from planning.models import HarvestEvent, NurseryEvent, Planting, PlanningYear
from reference.models import (
    Block,
    CropBySeason,
    CropInfo,
    CropSalesFormat,
    ProductRecipe,
    ProductRecipeComponent,
    SalesChannel,
)
from sales.models import SalesEvent

TEST_MIDDLEWARE = [
    middleware
    for middleware in settings.MIDDLEWARE
    if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
]

@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
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
        """Primary Crop Planner / plantings matrix surface (planning:matrix) must return 200."""
        response = self.client.get(reverse("planning:matrix"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Crop Planner")
        self.assertContains(response, "planning-matrix")

    def test_succession_create_route_renders_form_choices(self):
        response = self.client.get(reverse("planning:succession_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create Succession Series")
        self.assertContains(response, "Carrot")
        self.assertContains(response, "Field 1")

    def test_nursery_schedule_route_renders(self):
        response = self.client.get(reverse("planning:nursery_schedule"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nursery Schedule")

    def test_field_schedule_route_renders(self):
        response = self.client.get(reverse("planning:field_schedule"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Field Schedule")

    def test_planting_create_route_renders_form(self):
        response = self.client.get(reverse("planning:planting_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New Planting")
        self.assertContains(response, "Carrot")
        self.assertContains(response, "Field 1")

    def test_year_dependent_routes_redirect_when_no_active_year(self):
        PlanningYear.objects.all().delete()

        for route_name in [
            "planning:sales_plan",
            "planning:nursery_schedule",
            "planning:field_schedule",
            "planning:succession_create",
        ]:
            response = self.client.get(reverse(route_name))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse("planning:matrix"))


@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
class PlantingPkSmokeSuite(TestCase):
    """GET smoke for routes that need a real planting / harvest-event primary key."""

    @classmethod
    def setUpTestData(cls):
        planning_year = PlanningYear.objects.create(year=2026, status="active")
        cls.block = Block.objects.create(
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
        cls.planting = Planting.objects.create(
            planning_year=planning_year,
            crop=crop,
            crop_season=crop_season,
            block=cls.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=planting_date,
            planned_first_harvest_date=first_harvest,
            planned_last_harvest_date=last_harvest,
            planned_total_yield="120.00",
            status="planned",
        )
        cls.harvest_event = HarvestEvent.objects.create(
            planting=cls.planting,
            planned_date=first_harvest,
            planned_quantity="25.00",
            planned_units="pounds",
        )

    def test_planting_detail_edit_revise_prefilled_and_htmx_routes_return_200(self):
        pk = self.planting.pk
        block_id = self.block.pk
        checks = [
            ("planning:planting_detail", {"pk": pk}),
            ("planning:planting_edit", {"pk": pk}),
            ("planning:planting_revise", {"pk": pk}),
            ("planning:planting_create_prefilled", {"block_id": block_id, "week": 12}),
            ("planning:planting_detail_htmx", {"pk": pk}),
        ]
        for route_name, kwargs in checks:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name, kwargs=kwargs))
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers.get("Content-Type", ""))

    def test_operations_planting_scoped_routes_return_200(self):
        pk = self.planting.pk
        for route_name, kwargs in [
            ("operations:harvest_entry", {"pk": pk}),
            ("operations:field_walk", {"pk": pk}),
            (
                "operations:inventory_harvest_in",
                {"harvest_event_id": self.harvest_event.pk},
            ),
        ]:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name, kwargs=kwargs))
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers.get("Content-Type", ""))

    def test_planting_status_post_anonymous_redirects_to_admin_login(self):
        response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": self.planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])


@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
class PlanningHtmxHelperTests(TestCase):
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
            nursery_weeks=2,
            weeks_until_pot_up=1,
            seeds_per_cell=1,
            thinned_plants=0,
            units_per_bin=20,
            harvest_bin="crate",
        )
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot=Decimal("1.20"),
            harvest_weeks=6,
            dtm_days=65,
            rows_per_bed=3,
            tp_inrow_spacing=Decimal("0.50"),
        )
        planting_date = date(2026, 4, 1)
        first_harvest = planting_date + timedelta(days=cls.crop_season.dtm_days)
        last_harvest = first_harvest + timedelta(weeks=cls.crop_season.harvest_weeks - 1)
        cls.planting = Planting.objects.create(
            planning_year=cls.planning_year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            block=cls.block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=200,
            planned_plant_date=planting_date,
            planned_first_harvest_date=first_harvest,
            planned_last_harvest_date=last_harvest,
            planned_total_yield="240.00",
            status="planned",
        )

    def test_crop_season_options_returns_matching_options(self):
        response = self.client.get(
            reverse("planning:crop_season_options"),
            {"crop": self.crop.pk, "block": self.block.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DTM 65d")
        self.assertContains(response, "pounds/bf")

    def test_harvest_date_calc_returns_seed_and_harvest_dates(self):
        response = self.client.get(
            reverse("planning:harvest_date_calc"),
            {"crop_season": self.crop_season.pk, "planned_plant_date": "2026-04-15"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First harvest:")
        self.assertContains(response, "Last harvest:")
        self.assertContains(response, "Seed date:")

    def test_bedfeet_calc_returns_capacity_and_yield_details(self):
        response = self.client.get(
            reverse("planning:bedfeet_calc"),
            {
                "block": self.block.pk,
                "bed_start": 1,
                "bed_end": 2,
                "crop_season": self.crop_season.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bedfeet:")
        self.assertContains(response, "Planned yield:")
        self.assertContains(response, "Est. bins:")
        self.assertContains(response, "Plants:")

    def test_week_to_date_returns_real_harvest_calc_url(self):
        response = self.client.get(
            reverse("planning:week_to_date"),
            {"plant_week_input": 12},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="2026-03-16"')
        self.assertContains(response, f'hx-get="{reverse("planning:harvest_date_calc")}"')

    def test_bed_conflict_check_reports_conflict(self):
        response = self.client.get(
            reverse("planning:bed_conflict_check"),
            {
                "block": self.block.pk,
                "bed_start": 1,
                "bed_end": 1,
                "planned_plant_date": "2026-04-15",
                "crop_season": self.crop_season.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bed Conflicts:")
        self.assertContains(response, "Carrot")

    def test_bed_conflict_check_excludes_current_planting_on_edit(self):
        response = self.client.get(
            reverse("planning:bed_conflict_check"),
            {
                "block": self.block.pk,
                "bed_start": 1,
                "bed_end": 2,
                "planned_plant_date": "2026-04-01",
                "crop_season": self.crop_season.pk,
                "planting_id": self.planting.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No conflicts")


@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
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
            total_yield_per_bedfoot=Decimal("1.20"),
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

    def test_planting_edit_route_renders_existing_values(self):
        planting = self._create_planting(status="planned")

        response = self.client.get(
            reverse("planning:planting_edit", kwargs={"pk": planting.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit Planting")
        self.assertContains(response, "Carrot")
        self.assertContains(response, "Field 1")

    def test_planting_create_rejects_invalid_bed_range(self):
        response = self.client.post(
            reverse("planning:planting_create"),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "variety": "",
                "block": str(self.block.pk),
                "bed_start": "3",
                "bed_end": "1",
                "planned_plant_date": "2026-04-08",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bed end must be greater than or equal to bed start.")
        self.assertEqual(Planting.objects.filter(planning_year=self.planning_year).count(), 0)

    def test_planting_create_htmx_success_returns_trigger_headers(self):
        response = self.client.post(
            reverse("planning:planting_create"),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "variety": "",
                "block": str(self.block.pk),
                "bed_start": "1",
                "bed_end": "1",
                "planned_plant_date": "2026-04-08",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["HX-Trigger"], "plantingCreated")
        self.assertEqual(response.headers["HX-Redirect"], reverse("planning:matrix"))

    def test_planting_edit_rejects_bed_end_beyond_block_capacity(self):
        planting = self._create_planting(status="planned")

        response = self.client.post(
            reverse("planning:planting_edit", kwargs={"pk": planting.pk}),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "variety": "",
                "block": str(self.block.pk),
                "bed_start": "1",
                "bed_end": "99",
                "planned_plant_date": "2026-04-08",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "only has 10 beds")
        planting.refresh_from_db()
        self.assertEqual(planting.bed_end, 1)

    def test_planting_edit_htmx_success_returns_trigger_headers(self):
        planting = self._create_planting(status="planned")

        response = self.client.post(
            reverse("planning:planting_edit", kwargs={"pk": planting.pk}),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "variety": "",
                "block": str(self.block.pk),
                "bed_start": "1",
                "bed_end": "1",
                "planned_plant_date": "2026-04-08",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["HX-Trigger"], "plantingCreated")
        self.assertEqual(response.headers["HX-Redirect"], reverse("planning:matrix"))

    def test_planting_edit_replaces_pending_generated_events_instead_of_duplicating(self):
        self.crop.nursery_weeks = 2
        self.crop.weeks_until_pot_up = 1
        self.crop.save(update_fields=["nursery_weeks", "weeks_until_pot_up"])

        planting = self._create_planting(status="planned")
        planting.generate_nursery_events()
        planting.generate_harvest_events()

        original_nursery_count = NurseryEvent.objects.filter(planting=planting).count()
        original_harvest_count = HarvestEvent.objects.filter(planting=planting).count()

        response = self.client.post(
            reverse("planning:planting_edit", kwargs={"pk": planting.pk}),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "variety": "",
                "block": str(self.block.pk),
                "bed_start": "1",
                "bed_end": "1",
                "planned_plant_date": "2026-04-08",
            },
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(NurseryEvent.objects.filter(planting=planting).count(), original_nursery_count)
        self.assertEqual(HarvestEvent.objects.filter(planting=planting).count(), original_harvest_count)

    def test_planting_detail_htmx_route_renders_detail_panel(self):
        planting = self._create_planting(status="planned")

        response = self.client.get(
            reverse("planning:planting_detail_htmx", kwargs={"pk": planting.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Carrot")
        self.assertContains(response, "Record Harvest")

    def test_planting_revise_route_renders_revision_form(self):
        planting = self._create_planting(status="planned")

        response = self.client.get(
            reverse("planning:planting_revise", kwargs={"pk": planting.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revise Planting")
        self.assertContains(response, "Create Revision")

    def test_planting_revise_replaces_original_pending_events(self):
        self.crop.nursery_weeks = 2
        self.crop.weeks_until_pot_up = 1
        self.crop.save(update_fields=["nursery_weeks", "weeks_until_pot_up"])

        planting = self._create_planting(status="planned")
        planting.generate_nursery_events()
        planting.generate_harvest_events()

        response = self.client.post(
            reverse("planning:planting_revise", kwargs={"pk": planting.pk}),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "variety": "",
                "block": str(self.block.pk),
                "bed_start": "1",
                "bed_end": "1",
                "planned_plant_date": "2026-04-15",
                "succession_group": "",
                "notes": "Revised planting",
            },
        )
        self.assertEqual(response.status_code, 302)

        planting.refresh_from_db()
        self.assertEqual(planting.status, "revised")
        self.assertEqual(NurseryEvent.objects.filter(planting=planting).count(), 0)
        self.assertEqual(HarvestEvent.objects.filter(planting=planting).count(), 0)

        revised = Planting.objects.get(revision_of=planting)
        self.assertEqual(revised.status, "planned")
        self.assertGreater(NurseryEvent.objects.filter(planting=revised).count(), 0)
        self.assertGreater(HarvestEvent.objects.filter(planting=revised).count(), 0)

    def test_planting_revise_invalid_date_redirects_without_changing_original(self):
        planting = self._create_planting(status="planned")

        response = self.client.post(
            reverse("planning:planting_revise", kwargs={"pk": planting.pk}),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "block": str(self.block.pk),
                "bed_start": "1",
                "bed_end": "1",
                "planned_plant_date": "not-a-date",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("planning:planting_revise", kwargs={"pk": planting.pk}))
        planting.refresh_from_db()
        self.assertEqual(planting.status, "planned")
        self.assertFalse(Planting.objects.filter(revision_of=planting).exists())

    def test_planting_status_requires_staff(self):
        planting = self._create_planting(status="planned")
        self.client.logout()
        user = get_user_model().objects.create_user(
            "planner-nonstaff", "planner-nonstaff@example.com", "x", is_staff=False
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("planning:planting_status", kwargs={"pk": planting.pk}),
            {"status": "planted"},
        )
        self.assertEqual(response.status_code, 403)

    def test_planting_revise_requires_staff(self):
        planting = self._create_planting(status="planned")
        self.client.logout()
        user = get_user_model().objects.create_user(
            "reviser-nonstaff", "reviser-nonstaff@example.com", "x", is_staff=False
        )
        self.client.force_login(user)

        response = self.client.post(
            reverse("planning:planting_revise", kwargs={"pk": planting.pk}),
            {
                "crop": str(self.crop.pk),
                "crop_season": str(self.crop_season.pk),
                "block": str(self.block.pk),
                "bed_start": "1",
                "bed_end": "1",
                "planned_plant_date": "2026-04-15",
            },
        )
        self.assertEqual(response.status_code, 403)


@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
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
        self.assertContains(response, 'data-testid="fp-table-scroll"')
        self.assertContains(response, "fp-table-scroll")

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

    def test_sales_plan_context_marks_mix_products_without_hiding_non_mix_products(self):
        mix_recipe = ProductRecipe.objects.create(product=self.product, name="Salad Mix")
        ProductRecipeComponent.objects.create(
            recipe=mix_recipe,
            source_crop=self.product.crop,
            component_quantity=Decimal("1.00"),
            component_unit="pounds",
            component_percent=Decimal("100.00"),
        )

        response = self.client.get(reverse("planning:sales_plan"))
        self.assertEqual(response.status_code, 200)
        rows = response.context["product_rows"]
        carrot_row = next(r for r in rows if r["product"].id == self.product.id)
        mystery_row = next(r for r in rows if r["product"].id == self.no_profile_product.id)
        self.assertTrue(carrot_row["is_mix_product"])
        self.assertIsNotNone(carrot_row["active_recipe"])
        self.assertFalse(mystery_row["is_mix_product"])

    def test_sales_plan_row_subtitle_omits_crop_when_named_in_product_name(self):
        self.assertEqual(self.product.sales_plan_row_subtitle, "bunch")
        self.assertTrue(self.product.sales_plan_product_cell_one_line)
        self.assertEqual(self.winter_product.sales_plan_row_subtitle, "bag")
        self.assertEqual(self.no_profile_product.sales_plan_row_subtitle, "bunch")

    def test_sales_plan_row_subtitle_includes_crop_when_not_in_product_name(self):
        poultry = CropInfo.objects.create(
            name="Chicken",
            crop_type="Poultry",
            botanical_family="Phasianidae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="fresh",
            storage_weeks=0,
            harvest_unit="each",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        eggs = CropSalesFormat.objects.create(
            crop=poultry,
            product_name="Dozen Eggs",
            sale_price="6.00",
            sale_unit="dozen",
            harvest_qty_per_sale_unit="1.00",
            is_active=True,
        )
        self.assertEqual(eggs.sales_plan_row_subtitle, "Chicken · dozen")
        self.assertFalse(eggs.sales_plan_product_cell_one_line)
