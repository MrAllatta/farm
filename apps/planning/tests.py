"""Planning app tests."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from isoweek import Week

from operations.planting_display import planting_unit_code

from planning.models import HarvestEvent, PlanningYear, Planting
from planning.services.nursery_sheet_parity import run_nursery_parity
from reference.models import Block, CropBySeason, CropInfo, CropSalesFormat, SalesChannel, Variety
from sales.models import SalesEvent
from planning.services.planting_events_repair import repair_planting_events


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
            reverse("planning:sales_plan_by_channel"),
            {"channel": str(self.channel.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "In-season weekly order handoff")
        self.assertEqual(response.context["weekly_order_channel"], self.channel)
        self.assertIn("/sales/weekly-order/", response.context["weekly_order_url"])
        rows = response.context["product_rows"]
        row = next(r for r in rows if r["product"].id == self.product.id)
        cell20 = next(c for c in row["week_cells"] if c["week"] == 20)
        self.assertTrue(cell20["shortage"])
        self.assertEqual(cell20["shortage_magnitude"], Decimal("50.00"))
        self.assertGreater(row["row_total_demand"], Decimal("0"))
        self.assertGreater(row["row_total_supply"], Decimal("0"))
        self.assertIsNotNone(row["row_ratio"])


class EvenSplitSaleUnitsTests(TestCase):
    def test_sum_matches_total_with_remainder_to_first(self):
        from planning.services.sales_plan_allocation import even_split_sale_units

        parts = even_split_sale_units(Decimal("10.00"), 3)
        self.assertEqual(len(parts), 3)
        self.assertEqual(sum(parts), Decimal("10.00"))
        self.assertEqual(parts[0], Decimal("3.34"))
        self.assertEqual(parts[1], Decimal("3.33"))
        self.assertEqual(parts[2], Decimal("3.33"))


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


class CropPlannerMatrixSmokeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("crop-planner-smoke", password="pw", is_staff=True)
        self.year = PlanningYear.objects.create(year=2096, status="active")
        self.active_block = Block.objects.create(
            name="CP1",
            block_type="field",
            num_beds=8,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        Block.objects.create(
            name="CP2",
            block_type="field",
            num_beds=8,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=2,
        )
        self.crop = CropInfo.objects.create(
            name="Matrix Kale",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
        )
        self.crop_season = CropBySeason.objects.create(
            crop=self.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=21,
            rows_per_bed=4,
        )
        plant_date = Week(2096, 15).monday()
        Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.crop_season,
            block=self.active_block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=200,
            planned_plant_date=plant_date,
            planned_first_harvest_date=plant_date + timedelta(days=21),
            planned_last_harvest_date=plant_date + timedelta(days=42),
            planned_total_yield=Decimal("200.00"),
        )

    def test_matrix_route_shows_operator_guide_and_visible_summary(self):
        self.client.login(username="crop-planner-smoke", password="pw")
        response = self.client.get(reverse("planning:matrix_week", kwargs={"week": 15}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Workbook anchor: 402 Crop Planner")
        self.assertContains(response, "see everything")
        self.assertContains(response, "Visible plantings")
        self.assertContains(response, "1 / 2")


class SeasonRolloverServiceTests(TestCase):
    """season_rollover.copy_skeleton: +52 weeks, events, idempotency, dry-run."""

    def setUp(self):
        self.source = PlanningYear.objects.create(year=3101, status="active")
        self.target = PlanningYear.objects.create(year=3102, status="planning")
        self.block = Block.objects.create(
            name="R1",
            block_type="field",
            num_beds=8,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.crop = CropInfo.objects.create(
            name="Rollover Crop",
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
            total_yield_per_bedfoot=Decimal("2.00"),
            harvest_weeks=3,
            dtm_days=28,
            rows_per_bed=4,
        )
        self.p0 = Planting.objects.create(
            planning_year=self.source,
            crop=self.crop,
            crop_season=self.cs,
            block=self.block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=200,
            planned_plant_date=date(3101, 5, 4),
            planned_first_harvest_date=date(3101, 6, 1),
            planned_last_harvest_date=date(3101, 6, 15),
            planned_total_yield=Decimal("400.00"),
        )
        self.p0.generate_nursery_events()
        self.p0.generate_harvest_events()

    def test_dry_run_summary_counts(self):
        from planning.services.season_rollover import copy_skeleton

        out = copy_skeleton(self.source, self.target, dry_run=True)
        self.assertTrue(out.dry_run)
        self.assertEqual(out.num_plantings, 1)
        self.assertEqual(out.num_blocks, 1)
        self.assertEqual(out.total_bedfeet, 200)
        self.assertEqual(Planting.objects.filter(planning_year=self.target).count(), 0)

    def test_copy_shifts_dates_and_creates_events(self):
        from planning.services.season_rollover import copy_skeleton

        out = copy_skeleton(self.source, self.target, dry_run=False)
        self.assertFalse(out.dry_run)
        self.assertEqual(out.num_plantings, 1)

        np = Planting.objects.get(planning_year=self.target)
        self.assertEqual(np.planned_plant_date, date(3102, 5, 3))  # +timedelta(weeks=52)
        self.assertEqual(np.status, "planned")
        self.assertIsNone(np.actual_plant_date)
        self.assertGreater(np.harvest_events.count(), 0)

    def test_second_copy_refuses_when_target_has_plantings(self):
        from planning.services.season_rollover import copy_skeleton

        copy_skeleton(self.source, self.target, dry_run=False)
        with self.assertRaises(ValueError):
            copy_skeleton(self.source, self.target, dry_run=False)


class SeasonRolloverPreviewViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("rollover-preview", password="pw", is_staff=True)
        self.source = PlanningYear.objects.create(year=3110, status="active")
        PlanningYear.objects.create(year=3111, status="planning")

    def test_rollover_preview_shows_year_context_and_carryover_copy(self):
        self.client.login(username="rollover-preview", password="pw")
        session = self.client.session
        session["planning_year_id"] = self.source.id
        session.save()

        response = self.client.get(
            reverse("planning:season_rollover_preview"),
            {"target_year": "3111"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Working year context:")
        self.assertContains(response, "from prior year")


class PlantingDeleteViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("delstaff", password="pw", is_staff=True)
        self.year = PlanningYear.objects.create(year=3103, status="active")
        self.block = Block.objects.create(
            name="D1",
            block_type="field",
            num_beds=4,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.crop = CropInfo.objects.create(
            name="Del Crop",
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
            harvest_weeks=2,
            dtm_days=14,
            rows_per_bed=4,
        )
        self.planting = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.cs,
            block=self.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=date(3103, 4, 1),
            planned_first_harvest_date=date(3103, 4, 15),
            planned_last_harvest_date=date(3103, 4, 22),
            planned_total_yield=Decimal("100.00"),
        )

    def test_delete_requires_staff(self):
        c = Client()
        c.login(username="delstaff", password="pw")
        session = c.session
        session["planning_year_id"] = self.year.id
        session.save()
        url = reverse("planning:planting_delete")
        body = json.dumps({"planting_id": self.planting.pk})
        r = c.post(url, body, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.content.decode())
        self.assertTrue(data["ok"])
        self.assertFalse(Planting.objects.filter(pk=self.planting.pk).exists())


class CropPlannerPlantingCreateTemplateTests(TestCase):
    """Full-page vs HTMX templates for new planting (crop planner drawer / direct URL)."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("planform", password="pw", is_staff=True)
        self.year = PlanningYear.objects.create(year=2095, status="active")
        self.block = Block.objects.create(
            name="PF1",
            block_type="field",
            num_beds=6,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )

    def test_prefilled_create_full_page_includes_base_layout(self):
        self.client.login(username="planform", password="pw")
        session = self.client.session
        session["planning_year_id"] = self.year.id
        session.save()
        url = reverse(
            "planning:planting_create_prefilled",
            kwargs={"block_id": self.block.pk, "week": 20},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "main-nav")
        self.assertContains(r, "planting-form-page")

    def test_prefilled_create_htmx_returns_partial_without_nav(self):
        self.client.login(username="planform", password="pw")
        session = self.client.session
        session["planning_year_id"] = self.year.id
        session.save()
        url = reverse(
            "planning:planting_create_prefilled",
            kwargs={"block_id": self.block.pk, "week": 22},
        )
        r = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "planting-form")
        self.assertNotContains(r, "main-nav")


class SuccessionCreatePrefillTests(TestCase):
    """GET query params prefill succession form from crop planner range drag."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("succpref", password="pw", is_staff=True)
        self.year = PlanningYear.objects.create(year=2094, status="active")
        self.block = Block.objects.create(
            name="S1",
            block_type="field",
            num_beds=8,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )

    def test_get_prefills_block_weeks_and_block_type(self):
        self.client.login(username="succpref", password="pw")
        session = self.client.session
        session["planning_year_id"] = self.year.id
        session.save()
        url = (
            reverse("planning:succession_create")
            + f"?block={self.block.pk}&first_plant_week=10&last_plant_week=16&block_type=field"
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="id_first_plant_week"')
        self.assertContains(r, 'value="10"')
        self.assertContains(r, 'value="16"')
        self.assertContains(r, self.block.name)

    def test_get_htmx_returns_partial_without_nav(self):
        self.client.login(username="succpref", password="pw")
        session = self.client.session
        session["planning_year_id"] = self.year.id
        session.save()
        url = (
            reverse("planning:succession_create")
            + f"?block={self.block.pk}&first_plant_week=5&last_plant_week=8&block_type=field"
        )
        r = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "succession-form")
        self.assertNotContains(r, "main-nav")


class PlantingEventsRepairTests(TestCase):
    """OP-7 / LC-5 — backfill missing generated harvest and nursery events."""

    def setUp(self):
        self.year = PlanningYear.objects.create(year=2101, status="active")
        self.block = Block.objects.create(
            name="RepairBlock",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.crop_field = CropInfo.objects.create(
            name="Repair Crop Field",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
        )
        CropBySeason.objects.create(
            crop=self.crop_field,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        self.crop_season_field = CropBySeason.objects.get(crop=self.crop_field, block_type="field")

        self.crop_nursery = CropInfo.objects.create(
            name="Repair Crop Nursery",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=4,
            weeks_until_pot_up=2,
        )
        CropBySeason.objects.create(
            crop=self.crop_nursery,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        self.crop_season_nursery = CropBySeason.objects.get(crop=self.crop_nursery, block_type="field")

    def test_creates_harvest_events_when_missing(self):
        p = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop_field,
            crop_season=self.crop_season_field,
            block=self.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=date(2101, 5, 1),
            planned_first_harvest_date=date(2101, 6, 1),
            planned_last_harvest_date=date(2101, 6, 22),
            planned_total_yield=Decimal("100.00"),
        )
        self.assertEqual(p.harvest_events.count(), 0)
        stats = repair_planting_events(planning_year_ids=[self.year.id])
        self.assertEqual(stats.harvest_events_created_plantings, 1)
        self.assertGreater(p.harvest_events.count(), 0)

    def test_creates_nursery_events_when_crop_needs_nursery_and_none_exist(self):
        p = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop_nursery,
            crop_season=self.crop_season_nursery,
            block=self.block,
            bed_start=2,
            bed_end=2,
            planned_bedfeet=50,
            planned_plant_date=date(2101, 7, 1),
            planned_first_harvest_date=date(2101, 8, 1),
            planned_last_harvest_date=date(2101, 8, 22),
            planned_total_yield=Decimal("50.00"),
        )
        self.assertEqual(p.nursery_events.count(), 0)
        stats = repair_planting_events(planning_year_ids=[self.year.id])
        self.assertGreaterEqual(stats.nursery_events_created_plantings, 1)
        self.assertGreater(p.nursery_events.count(), 0)
        types = {e.event_type for e in p.nursery_events.all()}
        self.assertIn("seed", types)
        self.assertIn("transplant", types)

    def test_idempotent_second_run_does_not_duplicate(self):
        p = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop_field,
            crop_season=self.crop_season_field,
            block=self.block,
            bed_start=3,
            bed_end=3,
            planned_bedfeet=100,
            planned_plant_date=date(2101, 5, 10),
            planned_first_harvest_date=date(2101, 6, 10),
            planned_last_harvest_date=date(2101, 7, 1),
            planned_total_yield=Decimal("100.00"),
        )
        repair_planting_events(planning_year_ids=[self.year.id])
        n1 = p.harvest_events.count()
        repair_planting_events(planning_year_ids=[self.year.id])
        n2 = p.harvest_events.count()
        self.assertEqual(n1, n2)
        self.assertGreater(n1, 0)

    def test_skips_planting_with_existing_harvest_events(self):
        p = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop_field,
            crop_season=self.crop_season_field,
            block=self.block,
            bed_start=4,
            bed_end=4,
            planned_bedfeet=100,
            planned_plant_date=date(2101, 5, 1),
            planned_first_harvest_date=date(2101, 6, 1),
            planned_last_harvest_date=date(2101, 6, 22),
            planned_total_yield=Decimal("100.00"),
        )
        HarvestEvent.objects.create(
            planting=p,
            planned_date=date(2101, 6, 1),
            planned_quantity=Decimal("10"),
            planned_units="pounds",
        )
        before = p.harvest_events.count()
        stats = repair_planting_events(planning_year_ids=[self.year.id])
        self.assertEqual(stats.harvest_events_created_plantings, 0)
        self.assertEqual(p.harvest_events.count(), before)

    def test_fill_missing_planned_harvest_dates_derives_from_plant_date(self):
        """LC-5: mirror import gap where planned harvest dates never ran through full ``save()``."""
        p = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop_field,
            crop_season=self.crop_season_field,
            block=self.block,
            bed_start=5,
            bed_end=5,
            planned_bedfeet=100,
            planned_plant_date=date(2101, 5, 1),
            planned_first_harvest_date=date(2101, 6, 1),
            planned_last_harvest_date=date(2101, 6, 22),
            planned_total_yield=Decimal("100.00"),
        )
        p.planned_first_harvest_date = None
        p.planned_last_harvest_date = None
        self.assertTrue(p.fill_missing_planned_harvest_dates())
        self.assertEqual(
            p.planned_first_harvest_date,
            date(2101, 5, 1) + timedelta(days=self.crop_season_field.dtm_days),
        )
        span = max(int(self.crop_season_field.harvest_weeks or 0), 1) - 1
        self.assertEqual(
            p.planned_last_harvest_date,
            p.planned_first_harvest_date + timedelta(weeks=span),
        )

    def test_repair_fixes_inverted_harvest_window_and_creates_events(self):
        """LC-5: invalid first/last span in DB is normalized then weekly harvest rows are generated."""
        p = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop_field,
            crop_season=self.crop_season_field,
            block=self.block,
            bed_start=6,
            bed_end=6,
            planned_bedfeet=100,
            planned_plant_date=date(2101, 5, 1),
            planned_first_harvest_date=date(2101, 6, 10),
            planned_last_harvest_date=date(2101, 6, 24),
            planned_total_yield=Decimal("100.00"),
        )
        # Simulate legacy/partial DB writes that bypassed ``Planting.save()`` date normalization.
        Planting.objects.filter(pk=p.pk).update(planned_last_harvest_date=date(2101, 6, 3))
        p.refresh_from_db()
        self.assertLess(p.planned_last_harvest_date, p.planned_first_harvest_date)
        self.assertEqual(p.harvest_events.count(), 0)
        stats = repair_planting_events(planning_year_ids=[self.year.id])
        self.assertGreaterEqual(stats.harvest_planned_dates_filled, 1)
        self.assertEqual(stats.harvest_events_created_plantings, 1)
        p.refresh_from_db()
        self.assertGreaterEqual(p.planned_last_harvest_date, p.planned_first_harvest_date)
        self.assertGreater(p.harvest_events.count(), 0)


class PlantingImportBedRangeTests(TestCase):
    """``import_historical_data`` plantings path: consecutive-bed consolidation."""

    def setUp(self):
        self.year_int = 3088
        self.planning_year = PlanningYear.objects.create(year=self.year_int, status="planning")
        self.block = Block.objects.create(
            name="Z9",
            block_type="field",
            num_beds=20,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=99,
        )
        self.crop = CropInfo.objects.create(
            name="Zebra Chard",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="bunches",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
        )
        self.crop_season = CropBySeason.objects.create(
            crop=self.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )

    def _make_import_command(self):
        from core.management.commands.import_historical_data import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.stderr = StringIO()
        cmd.row_errors = []
        cmd.stats = defaultdict(
            lambda: {
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "error": 0,
                "processed": 0,
                "errors": 0,
            }
        )
        cmd.write_disabled = False
        cmd.validate_only = False
        cmd.planning_year_cache = {}
        cmd.crop_cache = {}
        cmd.block_cache = {}
        cmd.planting_cache = {}
        cmd.crop_season_cache = {}
        cmd.planning_year_cache[self.year_int] = self.planning_year
        return cmd

    def test_consecutive_beds_merge_into_one_planting(self):
        tmp = TemporaryDirectory()
        year_dir = Path(tmp.name) / f"year_{self.year_int}"
        year_dir.mkdir(parents=True)
        csv_path = year_dir / "plantings.csv"
        rows = [
            {
                "Crop": "Zebra Chard",
                "Variety": "Bright Lights",
                "Block": "Z9",
                "Bed Start": "1",
                "Bed End": "1",
                "Planned Plant Date": "3088-05-01",
                "Planned Bedfeet": "100",
                "Status": "Planned",
            },
            {
                "Crop": "Zebra Chard",
                "Variety": "Bright Lights",
                "Block": "Z9",
                "Bed Start": "2",
                "Bed End": "2",
                "Planned Plant Date": "3088-05-01",
                "Planned Bedfeet": "100",
                "Status": "Planned",
            },
            {
                "Crop": "Zebra Chard",
                "Variety": "Bright Lights",
                "Block": "Z9",
                "Bed Start": "3",
                "Bed End": "3",
                "Planned Plant Date": "3088-05-01",
                "Planned Bedfeet": "100",
                "Status": "Planned",
            },
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        cmd = self._make_import_command()
        cmd._import_plantings(self.year_int, str(year_dir))

        plantings = list(Planting.objects.filter(planning_year=self.planning_year))
        self.assertEqual(len(plantings), 1)
        p = plantings[0]
        self.assertEqual(p.bed_start, 1)
        self.assertEqual(p.bed_end, 3)
        self.assertEqual(p.planned_bedfeet, 300)
        self.assertEqual(p.variety, "Bright Lights")
        v = Variety.objects.filter(crop=self.crop, name__iexact="Bright Lights").first()
        self.assertIsNotNone(v)
        self.assertEqual(p.variety_obj_id, v.id)

    def test_non_consecutive_beds_remain_separate(self):
        tmp = TemporaryDirectory()
        year_dir = Path(tmp.name) / f"year_{self.year_int}"
        year_dir.mkdir(parents=True)
        csv_path = year_dir / "plantings.csv"
        rows = [
            {
                "Crop": "Zebra Chard",
                "Variety": "",
                "Block": "Z9",
                "Bed Start": "1",
                "Bed End": "1",
                "Planned Plant Date": "3088-05-01",
                "Planned Bedfeet": "100",
                "Status": "Planned",
            },
            {
                "Crop": "Zebra Chard",
                "Variety": "",
                "Block": "Z9",
                "Bed Start": "4",
                "Bed End": "4",
                "Planned Plant Date": "3088-05-01",
                "Planned Bedfeet": "100",
                "Status": "Planned",
            },
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        cmd = self._make_import_command()
        cmd._import_plantings(self.year_int, str(year_dir))
        self.assertEqual(Planting.objects.filter(planning_year=self.planning_year).count(), 2)


class NurseryParityAndDerivedOnlyTests(TestCase):
    def test_run_nursery_parity_missing_file(self):
        with TemporaryDirectory() as tmp:
            r = run_nursery_parity(tmp, 2099)
            self.assertEqual(r["status"], "skipped")

    def test_import_nursery_events_ignores_csv(self):
        from core.management.commands.import_historical_data import Command

        tmp = TemporaryDirectory()
        year_dir = Path(tmp.name) / "year_2020"
        year_dir.mkdir(parents=True)
        (year_dir / "nursery_events.csv").write_text(
            "CROP & VARIETY,Nursery Seeding Year,Nursery Seeding Week,Plan Field Week\n"
            "Kale,2020,1,20\n",
            encoding="utf-8",
        )
        out = StringIO()
        err = StringIO()
        cmd = Command(stdout=out, stderr=err)
        cmd._import_nursery_events(2020, str(year_dir))
        self.assertIn("ignored", out.getvalue().lower())


class PlantingUnitCodeTests(TestCase):
    """Durable ``planting_code`` (e.g. P-2101-0001) on create and sequential allocation."""

    def test_planting_code_assigned_on_create_and_increments(self):
        year = PlanningYear.objects.create(year=2101, status="active")
        block = Block.objects.create(
            name="CodeBlock",
            block_type="field",
            num_beds=8,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        crop = CropInfo.objects.create(
            name="CodeCrop",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
        )
        cs = CropBySeason.objects.create(
            crop=crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=21,
            rows_per_bed=4,
        )
        d0 = date(2101, 5, 1)
        d1 = date(2101, 5, 15)
        p1 = Planting.objects.create(
            planning_year=year,
            crop=crop,
            crop_season=cs,
            block=block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=50,
            planned_plant_date=d0,
            planned_first_harvest_date=d0 + timedelta(weeks=3),
            planned_last_harvest_date=d0 + timedelta(weeks=6),
            planned_total_yield=Decimal("50"),
        )
        p2 = Planting.objects.create(
            planning_year=year,
            crop=crop,
            crop_season=cs,
            block=block,
            bed_start=2,
            bed_end=2,
            planned_bedfeet=50,
            planned_plant_date=d1,
            planned_first_harvest_date=d1 + timedelta(weeks=3),
            planned_last_harvest_date=d1 + timedelta(weeks=6),
            planned_total_yield=Decimal("50"),
        )
        self.assertEqual(p1.planting_code, "P-2101-0001")
        self.assertEqual(p2.planting_code, "P-2101-0002")
        self.assertEqual(planting_unit_code(p1), "P-2101-0001")
        self.assertEqual(planting_unit_code(p2), "P-2101-0002")
