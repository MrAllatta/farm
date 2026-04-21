"""Planning app tests."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from isoweek import Week

from planning.models import HarvestEvent, PlanningYear, Planting
from reference.models import Block, CropBySeason, CropInfo, CropSalesFormat, SalesChannel
from sales.models import SalesEvent


class SalesPlanShortageTests(TestCase):
    def setUp(self):
        self.year = PlanningYear.objects.create(year=2098, status="active")
        self.channel = SalesChannel.objects.create(
            name="Stand",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target="100.00",
            is_csa=False,
            allocation_priority=1,
        )
        self.block = Block.objects.create(
            name="B1",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.crop = CropInfo.objects.create(
            name="Shortage Crop",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
        )
        CropBySeason.objects.create(
            crop=self.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        self.product = CropSalesFormat.objects.create(
            crop=self.crop,
            product_name="Shortage Crop bunches",
            sale_price=Decimal("3.00"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("2.00"),
        )
        self.crop_season = CropBySeason.objects.get(crop=self.crop, block_type="field")
        self.planting = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.crop_season,
            block=self.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=date(2098, 5, 1),
            planned_first_harvest_date=date(2098, 6, 1),
            planned_last_harvest_date=date(2098, 6, 28),
            planned_total_yield=Decimal("100.00"),
        )
        wk = 20
        mon = Week(2098, wk).monday()
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.year,
            channel=self.channel,
            product=self.product,
            sale_date=mon,
            planned_quantity=Decimal("100.00"),
            planned_revenue=Decimal("300.00"),
        )
        HarvestEvent.objects.create(
            planting=self.planting,
            planned_date=mon,
            planned_quantity=Decimal("100.00"),
            planned_units="pounds",
        )

    def test_shortage_magnitude_and_row_totals(self):
        response = self.client.get(
            reverse("planning:sales_plan"),
            {"channel": str(self.channel.pk)},
        )
        self.assertEqual(response.status_code, 200)
        rows = response.context["product_rows"]
        row = next(r for r in rows if r["product"].id == self.product.id)
        cell20 = next(c for c in row["week_cells"] if c["week"] == 20)
        self.assertTrue(cell20["shortage"])
        self.assertEqual(cell20["shortage_magnitude"], Decimal("50.00"))
        self.assertGreater(row["row_total_demand"], Decimal("0"))
        self.assertGreater(row["row_total_supply"], Decimal("0"))
        self.assertIsNotNone(row["row_ratio"])


class PlantingMoveViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("mover", password="pw", is_staff=True)
        self.year = PlanningYear.objects.create(year=2097, status="active")
        self.b1 = Block.objects.create(
            name="M1",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.b2 = Block.objects.create(
            name="M2",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=2,
        )
        self.crop = CropInfo.objects.create(
            name="Move Crop",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
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
        self.planting = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.cs,
            block=self.b1,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=200,
            planned_plant_date=Week(2097, 15).monday(),
            planned_first_harvest_date=Week(2097, 15).monday() + timedelta(days=30),
            planned_last_harvest_date=Week(2097, 15).monday() + timedelta(days=30 + 7 * 3),
            planned_total_yield=Decimal("200.00"),
        )

    def test_move_requires_staff(self):
        c = Client()
        url = reverse("planning:planting_move")
        body = json.dumps(
            {
                "planting_id": self.planting.pk,
                "block_id": self.b1.pk,
                "week": 20,
                "from_block_id": self.b1.pk,
                "matrix_date": str(Week(2097, 15).monday()),
            }
        )
        r = c.post(url, body, content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_same_block_move_returns_html_without_reload(self):
        c = Client()
        c.login(username="mover", password="pw")
        url = reverse("planning:planting_move")
        center = Week(2097, 15).monday().isoformat()
        body = json.dumps(
            {
                "planting_id": self.planting.pk,
                "block_id": self.b1.pk,
                "week": 18,
                "from_block_id": self.b1.pk,
                "matrix_date": center,
            }
        )
        r = c.post(url, body, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.content.decode())
        self.assertTrue(data["ok"])
        self.assertFalse(data["reload"])
        self.assertIn("planting-bar", data["html"])
        self.planting.refresh_from_db()
        self.assertEqual(self.planting.planned_plant_date, Week(2097, 18).monday())

    def test_block_change_requests_reload(self):
        c = Client()
        c.login(username="mover", password="pw")
        url = reverse("planning:planting_move")
        center = Week(2097, 15).monday().isoformat()
        body = json.dumps(
            {
                "planting_id": self.planting.pk,
                "block_id": self.b2.pk,
                "week": 15,
                "from_block_id": self.b1.pk,
                "matrix_date": center,
            }
        )
        r = c.post(url, body, content_type="application/json")
        data = json.loads(r.content.decode())
        self.assertTrue(data["ok"])
        self.assertTrue(data["reload"])
        self.assertEqual(data.get("html"), "")
        self.planting.refresh_from_db()
        self.assertEqual(self.planting.block_id, self.b2.pk)
