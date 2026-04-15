from datetime import date, timedelta

from django.test import TestCase

from planning.models import Planting, PlanningYear
from reference.models import Block, CropBySeason, CropInfo


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
