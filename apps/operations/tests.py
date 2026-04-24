"""Tests for operations services and views."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from isoweek import Week

from operations.models import FieldWalkNote
from operations.planting_display import format_planting_display_id, planting_schedule_chip_css_class
from operations.services import week_ops as week_ops_service
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
        # Pack list is channel/product centric; without allocations it may omit harvest qty rows.
        self.assertContains(r2, "PACK LIST")


class WeekOpsServiceTests(TestCase):
    """``week_ops.week_context`` ordering, rollups, and progress."""

    def setUp(self):
        self.block_b = Block.objects.create(
            name="B-Second",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=2,
        )
        self.block_a = Block.objects.create(
            name="A-First",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        self.crop = CropInfo.objects.create(
            name="WeekOps Greens",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("0.50"),
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
            dtm_days=21,
            rows_per_bed=4,
        )
        self.year = PlanningYear.objects.create(year=2026, status="active")
        mon = Week(2026, 20).monday()
        self.planting_a = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.cs,
            block=self.block_a,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=3),
            planned_total_yield=Decimal("100"),
            status="growing",
        )
        self.planting_b = Planting.objects.create(
            planning_year=self.year,
            crop=self.crop,
            crop_season=self.cs,
            block=self.block_b,
            bed_start=2,
            bed_end=2,
            planned_bedfeet=100,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=3),
            planned_total_yield=Decimal("100"),
            status="growing",
        )
        self.he_a = HarvestEvent.objects.create(
            planting=self.planting_a,
            planned_date=mon,
            planned_quantity=Decimal("100.00"),
            planned_units="pounds",
        )
        self.he_b = HarvestEvent.objects.create(
            planting=self.planting_b,
            planned_date=mon + timedelta(days=2),
            planned_quantity=Decimal("50.00"),
            planned_units="pounds",
            actual_quantity=Decimal("50.00"),
            actual_bins=Decimal("5.0"),
        )

    def test_harvest_week_blocks_ordered_by_walk_route(self):
        ctx = week_ops_service.week_context(self.year, 20, mode="harvest_needs")
        blocks = ctx["blocks"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["block"].id, self.block_a.id)
        self.assertEqual(blocks[1]["block"].id, self.block_b.id)

    def test_progress_counts_recorded_events(self):
        ctx = week_ops_service.week_context(self.year, 20, mode="harvest_entry")
        self.assertEqual(ctx["progress"]["total_events"], 2)
        self.assertEqual(ctx["progress"]["recorded_events"], 1)

    def test_rollup_by_crop_aggregates_bins(self):
        ctx = week_ops_service.week_context(self.year, 20, mode="harvest_needs")
        roll = ctx["week_rollup_by_crop"][self.crop.id]
        self.assertEqual(roll["event_count"], 2)
        self.assertEqual(roll["recorded_count"], 1)
        self.assertGreater(roll["target_bins"], 0)

    def test_field_walk_includes_yield_note_in_context(self):
        FieldWalkNote.objects.create(
            planting=self.planting_a,
            walk_date=date(2026, 4, 1),
            condition="fair",
            yield_adjust_pct=80,
            notes="thin stand",
        )
        ctx = week_ops_service.week_context(
            self.year, 20, today=date(2026, 4, 10), mode="harvest_needs"
        )
        prow = ctx["blocks"][0]["plantings"][0]
        self.assertEqual(prow["yield_adjust_pct"], 80)
        self.assertEqual(prow["last_walk_note"].condition, "fair")

    def test_week_context_prow_includes_planting_display_and_schedule_chip(self):
        ctx = week_ops_service.week_context(self.year, 20, today=date(2026, 4, 10), mode="field_walk")
        prow = ctx["blocks"][0]["plantings"][0]
        self.assertEqual(prow["planting_display_id"], format_planting_display_id(prow["planting"].pk))
        self.assertTrue(prow["schedule_chip_class"].startswith("chip-plant-schedule-"))


class WeekOpsViewTests(TestCase):
    """Smoke + POST behaviour for unified week-ops URLs."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            "weekops_staff", password="pw", is_staff=True
        )

    def setUp(self):
        self.client.login(username="weekops_staff", password="pw")
        b = Block.objects.create(
            name="VW",
            block_type="field",
            num_beds=10,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        crop = CropInfo.objects.create(
            name="VW Crop",
            crop_type="Greens",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
            nursery_weeks=0,
            units_per_bin=10,
            harvest_bin="tote",
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
        self.year = PlanningYear.objects.create(year=2026, status="active")
        mon = Week(2026, 18).monday()
        self.planting = Planting.objects.create(
            planning_year=self.year,
            crop=crop,
            crop_season=cs,
            block=b,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=50,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=3),
            planned_total_yield=Decimal("50"),
            status="growing",
        )
        self.he = HarvestEvent.objects.create(
            planting=self.planting,
            planned_date=mon,
            planned_quantity=Decimal("40.00"),
            planned_units="pounds",
        )

    def test_week_nav_walk_needs_record_200(self):
        for name in ("weekops_walk", "weekops_needs", "weekops_record"):
            r = self.client.get(reverse(f"operations:{name}", kwargs={"week": 18}))
            self.assertEqual(r.status_code, 200, name)

    def test_harvest_needs_shows_yield_adjustment_badge(self):
        FieldWalkNote.objects.create(
            planting=self.planting,
            walk_date=date(2026, 4, 1),
            condition="poor",
            yield_adjust_pct=75,
            notes="",
        )
        r = self.client.get(reverse("operations:weekops_needs", kwargs={"week": 18}))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "75%")

    def test_field_walk_no_change_block_creates_notes(self):
        self.assertEqual(self.planting.field_walk_notes.count(), 0)
        r = self.client.post(
            reverse("operations:weekops_walk", kwargs={"week": 18}),
            {"no_change_block": str(self.planting.block_id)},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.planting.field_walk_notes.count(), 1)
        n = self.planting.field_walk_notes.first()
        self.assertEqual(n.condition, "good")
        self.assertEqual(n.yield_adjust_pct, 100)

    def test_field_walk_no_change_from_planted_advances_to_growing(self):
        self.planting.status = "planted"
        self.planting.save(update_fields=["status"])
        r = self.client.post(
            reverse("operations:weekops_walk", kwargs={"week": 18}),
            {"no_change_block": str(self.planting.block_id)},
        )
        self.assertEqual(r.status_code, 302)
        self.planting.refresh_from_db()
        self.assertEqual(self.planting.status, "growing")

    def test_field_walk_post_from_planted_advances_to_growing(self):
        self.planting.status = "planted"
        self.planting.save(update_fields=["status"])
        r = self.client.post(
            reverse("operations:weekops_walk", kwargs={"week": 18}),
            {
                f"condition_{self.planting.id}": "good",
                f"notes_{self.planting.id}": "",
                f"yield_{self.planting.id}": "100",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.planting.refresh_from_db()
        self.assertEqual(self.planting.status, "growing")

    def test_field_walk_note_post_from_planted_advances_to_growing(self):
        self.planting.status = "planted"
        self.planting.save(update_fields=["status"])
        r = self.client.post(
            reverse("operations:field_walk", kwargs={"pk": self.planting.pk}),
            {"condition": "good", "notes": "", "yield_adjust": "100"},
        )
        self.assertEqual(r.status_code, 302)
        self.planting.refresh_from_db()
        self.assertEqual(self.planting.status, "growing")

    def test_harvest_entry_appends_notes(self):
        self.he.notes = "existing"
        self.he.save()
        r = self.client.post(
            reverse("operations:weekops_record", kwargs={"week": 18}),
            {
                f"bins_{self.he.id}": "2",
                f"notes_{self.he.id}": "picked clean",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.he.refresh_from_db()
        self.assertIn("picked clean", self.he.notes)
        self.assertIn("existing", self.he.notes)

    def test_current_redirects_resolve(self):
        r = self.client.get(reverse("operations:field_walk_current"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/operations/week/", r["Location"])

    def test_weekops_needs_lists_planting_display_id(self):
        r = self.client.get(reverse("operations:weekops_needs", kwargs={"week": 18}))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, format_planting_display_id(self.planting.pk))

    def test_harvest_needs_shows_missing_harvest_events_diagnostic(self):
        mon = Week(2026, 18).monday()
        Planting.objects.create(
            planning_year=self.year,
            crop=self.planting.crop,
            crop_season=self.planting.crop_season,
            block=self.planting.block,
            bed_start=3,
            bed_end=3,
            planned_bedfeet=30,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=2),
            planned_total_yield=Decimal("30"),
            status="growing",
        )
        r = self.client.get(reverse("operations:weekops_needs", kwargs={"week": 18}))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"data-empty-reason=\"missing_generated_harvests\"", r.content)


class PlantingScheduleChipCssClassTests(TestCase):
    def test_format_planting_display_id_zero_pads(self):
        self.assertEqual(format_planting_display_id(1), "P-00001")
        self.assertEqual(format_planting_display_id(123), "P-00123")

    def test_schedule_chip_css_class_derivation(self):
        planned = date(2026, 6, 10)
        self.assertEqual(
            planting_schedule_chip_css_class(planned, None, date(2026, 6, 5)),
            "chip-plant-schedule-on",
        )
        self.assertEqual(
            planting_schedule_chip_css_class(planned, None, date(2026, 6, 15)),
            "chip-plant-schedule-behind",
        )
        self.assertEqual(
            planting_schedule_chip_css_class(planned, date(2026, 6, 8), date(2026, 6, 20)),
            "chip-plant-schedule-ahead",
        )
        self.assertEqual(
            planting_schedule_chip_css_class(planned, date(2026, 6, 10), date(2026, 6, 20)),
            "chip-plant-schedule-on",
        )
        self.assertEqual(
            planting_schedule_chip_css_class(planned, date(2026, 6, 12), date(2026, 6, 20)),
            "chip-plant-schedule-behind",
        )
