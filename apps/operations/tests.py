"""Tests for operations services and views."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from isoweek import Week

from operations.services.field_walk_cascade import apply_yield_adjustment_to_future_harvests
from planning.models import HarvestEvent, PlanningYear, Planting
from reference.models import Block, CropBySeason, CropInfo


class FieldWalkCascadeTests(TestCase):
    def setUp(self):
        self.block = Block.objects.create(
            name="T1",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.crop = CropInfo.objects.create(
            name="Test Greens",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("0.50"),
            nursery_weeks=0,
        )
        self.crop_season = CropBySeason.objects.create(
            crop=self.crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot=Decimal("2.00"),
            harvest_weeks=4,
            dtm_days=21,
            rows_per_bed=4,
        )
        self.year = PlanningYear.objects.create(year=2099, status="active")
        plant_date = date(2099, 6, 1)
        self.planting = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.crop_season,
            block=self.block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=200,
            planned_plant_date=plant_date,
            planned_first_harvest_date=plant_date + timedelta(days=21),
            planned_last_harvest_date=plant_date + timedelta(days=21 + 7 * 3),
            planned_total_yield=Decimal("400.00"),
        )
        self.past = HarvestEvent.objects.create(
            planting=self.planting,
            planned_date=plant_date,
            planned_quantity=Decimal("50.00"),
            planned_units="pounds",
        )
        self.future_open = HarvestEvent.objects.create(
            planting=self.planting,
            planned_date=plant_date + timedelta(days=60),
            planned_quantity=Decimal("100.00"),
            planned_units="pounds",
        )
        self.future_logged = HarvestEvent.objects.create(
            planting=self.planting,
            planned_date=plant_date + timedelta(days=67),
            planned_quantity=Decimal("100.00"),
            planned_units="pounds",
            actual_quantity=Decimal("10.00"),
        )

    def test_yield_adjustment_scales_future_open_events_only(self):
        anchor = self.planting.planned_plant_date + timedelta(days=30)
        n = apply_yield_adjustment_to_future_harvests(
            self.planting, 80, from_date=anchor
        )
        self.assertEqual(n, 1)
        self.past.refresh_from_db()
        self.future_open.refresh_from_db()
        self.future_logged.refresh_from_db()
        self.assertEqual(self.past.planned_quantity, Decimal("50.00"))
        self.assertEqual(self.future_open.planned_quantity, Decimal("80.00"))
        self.assertEqual(self.future_logged.planned_quantity, Decimal("100.00"))

    def test_hundred_percent_is_no_op(self):
        n = apply_yield_adjustment_to_future_harvests(self.planting, 100)
        self.assertEqual(n, 0)


class FieldWalkCascadePrintReportsTests(TestCase):
    """Harvest list and pack list read cascaded HarvestEvent.planned_quantity."""

    def setUp(self):
        self.year = PlanningYear.objects.create(year=3033, status="active")
        self.block = Block.objects.create(
            name="R1",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.crop = CropInfo.objects.create(
            name="PrintCrop",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
            units_per_bin=10,
            harvest_bin="tote",
        )
        self.cs = CropBySeason.objects.create(
            crop=self.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        from reference.models import CropSalesFormat

        CropSalesFormat.objects.create(
            crop=self.crop,
            product_name="PrintCrop bunch",
            sale_price=Decimal("2.00"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("1.00"),
        )
        mon = Week(3033, 22).monday()
        self.planting = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.cs,
            block=self.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=mon,
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=3),
            planned_total_yield=Decimal("100"),
        )
        self.he = HarvestEvent.objects.create(
            planting=self.planting,
            planned_date=mon,
            planned_quantity=Decimal("100.00"),
            planned_units="pounds",
        )

    def test_harvest_and_pack_list_reflect_yield_cascade(self):
        apply_yield_adjustment_to_future_harvests(self.planting, 80, from_date=self.he.planned_date)
        wk = self.he.planned_date.isocalendar()[1]
        r1 = self.client.get(reverse("reports:harvest_list_print", kwargs={"week": wk}))
        r2 = self.client.get(reverse("reports:pack_list_print", kwargs={"week": wk}))
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r1, "80")
        self.assertContains(r2, "80")
