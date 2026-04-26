from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from isoweek import Week

from operations.models import FieldWalkNote, InventoryLedger, PackBatch
from reference.models import CropBySeason, CropInfo, CropSalesFormat, SalesCategory, SalesChannel
from reference.sales_rollups import plan_week_iso_counts
from planning.models import HarvestEvent, Planting, PlanningYear
from sales.models import QuickSalesEntry, SalesEvent


class SalesModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.channel = SalesChannel.objects.create(
            name="Farm Stand",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("500.00"),
            is_csa=False,
            allocation_priority=1,
        )
        crop = CropInfo.objects.create(
            name="Carrot",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            fresh_or_storage="fresh",
            harvest_unit="bunch",
            avg_unit_weight=Decimal("1.00"),
        )
        cls.product = CropSalesFormat.objects.create(
            crop=crop,
            product_name="Carrot Bunch",
            sale_price=Decimal("3.50"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            sku="CAR-BUN",
            is_active=True,
        )
        cls.planning_year = PlanningYear.objects.create(year=2026, status="planning")

    def test_sales_event_sell_through_uses_actual_quantity_when_present(self):
        event = SalesEvent.objects.create(
            channel=self.channel,
            sale_date=date(2026, 3, 14),
            product=self.product,
            actual_quantity=Decimal("8.00"),
            brought_quantity=Decimal("10.00"),
            returned_quantity=Decimal("3.00"),
        )

        self.assertEqual(event.sell_through_pct, Decimal("80.0"))
        self.assertEqual(event.sale_week, 11)

    def test_sales_event_sell_through_falls_back_to_brought_minus_returned(self):
        event = SalesEvent.objects.create(
            channel=self.channel,
            sale_date=date(2026, 3, 14),
            product=self.product,
            brought_quantity=Decimal("10.00"),
            returned_quantity=Decimal("2.00"),
        )

        self.assertEqual(event.sell_through_pct, Decimal("80.0"))

    def test_quick_sales_entry_total_revenue_sums_cash_and_card(self):
        quick_entry = QuickSalesEntry.objects.create(
            channel=self.channel,
            sale_date=date(2026, 3, 14),
            total_cash=Decimal("42.50"),
            total_card=Decimal("57.50"),
        )

        self.assertEqual(quick_entry.total_revenue, Decimal("100.00"))

    def test_sales_event_plan_entry_kind_and_planning_year(self):
        event = SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.planning_year,
            channel=self.channel,
            sale_date=date(2026, 5, 5),
            product=self.product,
            planned_quantity=Decimal("12.00"),
            planned_revenue=Decimal("42.00"),
        )
        self.assertEqual(event.entry_kind, SalesEvent.EntryKind.PLAN)
        self.assertEqual(event.planning_year, self.planning_year)

    def test_sales_event_plan_and_actual_can_share_same_date_product(self):
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.planning_year,
            channel=self.channel,
            sale_date=date(2026, 6, 1),
            product=self.product,
            planned_quantity=Decimal("10.00"),
            planned_revenue=Decimal("35.00"),
        )
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            channel=self.channel,
            sale_date=date(2026, 6, 1),
            product=self.product,
            actual_quantity=Decimal("9.00"),
            actual_revenue=Decimal("31.50"),
        )
        self.assertEqual(SalesEvent.objects.count(), 2)

    def test_sales_event_can_link_to_pack_batch_for_mix_traceability(self):
        pack_batch = PackBatch.objects.create(
            product=self.product,
            packed_quantity=Decimal("15.00"),
            packed_unit="bunch",
            pack_date=date(2026, 6, 1),
        )
        event = SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            channel=self.channel,
            sale_date=date(2026, 6, 1),
            product=self.product,
            pack_batch=pack_batch,
            actual_quantity=Decimal("9.00"),
            actual_revenue=Decimal("31.50"),
        )
        self.assertEqual(event.pack_batch, pack_batch)

    def test_actual_sales_event_writes_sale_out_ledger(self):
        SalesEvent.objects.create(
            channel=self.channel,
            sale_date=date(2026, 7, 1),
            product=self.product,
            actual_quantity=Decimal("4.00"),
            actual_revenue=Decimal("14.00"),
        )
        le = InventoryLedger.objects.filter(crop=self.product.crop, event_type="sale_out").first()
        self.assertIsNotNone(le)
        self.assertEqual(le.quantity, Decimal("-4.00"))

    def test_return_in_ledger_and_resale_drawn_from_return(self):
        prior = SalesEvent.objects.create(
            channel=self.channel,
            sale_date=date(2026, 7, 1),
            product=self.product,
            actual_quantity=Decimal("10.00"),
            brought_quantity=Decimal("12.00"),
            returned_quantity=Decimal("2.00"),
        )
        ret = InventoryLedger.objects.filter(
            crop=self.product.crop, event_type="return_in"
        ).order_by("-id").first()
        self.assertIsNotNone(ret)
        self.assertEqual(ret.quantity, Decimal("2.00"))

        resale = SalesEvent.objects.create(
            channel=self.channel,
            sale_date=date(2026, 7, 8),
            product=self.product,
            actual_quantity=Decimal("1.50"),
            drawn_from_return=prior,
        )
        self.assertEqual(resale.drawn_from_return_id, prior.pk)


class WeeklyChannelOrderViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from reference.models import Block

        User = get_user_model()
        cls.staff = User.objects.create_user("weekly_order_staff", password="pw", is_staff=True)
        cls.channel = SalesChannel.objects.create(
            name="Farmers Market",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("400.00"),
            is_csa=False,
            allocation_priority=1,
        )
        cls.py_2024 = PlanningYear.objects.create(year=2024, status="complete")
        cls.py_2026 = PlanningYear.objects.create(year=2026, status="active")
        crop = CropInfo.objects.create(
            name="Kale",
            crop_type="Greens",
            botanical_family="Brassicaceae",
            fresh_or_storage="fresh",
            harvest_unit="bunch",
            avg_unit_weight=Decimal("0.40"),
        )
        cls.product = CropSalesFormat.objects.create(
            crop=crop,
            product_name="Kale Bunch",
            sale_price=Decimal("4.00"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            sku="KAL-BUN",
            is_active=True,
        )
        cls.block = Block.objects.create(
            name="B1",
            block_type="field",
            num_beds=8,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )

    def setUp(self):
        self.client.login(username="weekly_order_staff", password="pw")
        session = self.client.session
        session["planning_year_id"] = self.py_2026.id
        session.save()

    def test_weekly_order_shows_historical_same_iso_week(self):
        wk = 20
        mon_2024 = Week(2024, wk).monday()
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            channel=self.channel,
            sale_date=mon_2024,
            product=self.product,
            actual_quantity=Decimal("12"),
            actual_revenue=Decimal("48.00"),
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"data-historical-empty=", r.content)
        self.assertIn(b"12.0", r.content)

    def test_weekly_order_smoke_shows_handoff_and_empty_state_copy(self):
        wk = 21
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Weekly handoff")
        self.assertContains(r, "data-weekly-order-empty")
        self.assertContains(r, "Field walk notes")
        self.assertContains(r, "Harvest needs")
        self.assertContains(r, "Record harvest")
        self.assertContains(r, "Market sales entry")

    def test_weekly_order_shows_field_walk_availability_hint(self):
        wk = 24
        mon = Week(2026, wk).monday()
        crop_season = CropBySeason.objects.create(
            crop=self.product.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        planting = Planting.objects.create(
            planning_year=self.py_2026,
            crop=self.product.crop,
            crop_season=crop_season,
            block=self.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=100,
            planned_plant_date=mon - timedelta(days=30),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=3),
            planned_total_yield=Decimal("100.00"),
        )
        HarvestEvent.objects.create(
            planting=planting,
            planned_date=mon,
            planned_quantity=Decimal("30.00"),
            planned_units="bunch",
        )
        FieldWalkNote.objects.create(
            planting=planting,
            walk_date=mon,
            condition="fair",
            yield_adjust_pct=80,
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Last field walk")
        self.assertContains(r, "fair, 80% yield")
        # LIVE-4: harvest rollup → ~sale units (30 planned ÷ 1 bunch/sale unit)
        self.assertContains(r, "30.0")

    def test_weekly_order_harvest_supply_nonzero_after_repair(self):
        """LIVE-4 consumer path: repair creates HarvestEvent rows; supply column is non-zero."""
        from planning.services.planting_events_repair import repair_planting_events

        wk = 31
        mon = Week(2026, wk).monday()
        crop_season = CropBySeason.objects.create(
            crop=self.product.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        Planting.objects.create(
            planning_year=self.py_2026,
            crop=self.product.crop,
            crop_season=crop_season,
            block=self.block,
            bed_start=3,
            bed_end=3,
            planned_bedfeet=40,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=2),
            planned_total_yield=Decimal("40.00"),
            status="growing",
        )
        stats = repair_planting_events(planning_year_ids=[self.py_2026.id])
        self.assertGreaterEqual(stats.harvest_events_created_plantings, 1)
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"data-weekly-supply-all-zero=", r.content)
        # weekly_yield_per_bedfoot = 1/4; * 40 bedfeet => 10 per pick
        self.assertContains(r, "10.0")

    def test_weekly_order_zero_supply_explains_iso_week_when_events_elsewhere(self):
        """LIVE-4: plantings have harvest events, but none in the requested ISO week — honest empty copy."""
        wk_view = 15
        mon_pick = Week(2026, 30).monday()
        crop_season = CropBySeason.objects.create(
            crop=self.product.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("4.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        planting = Planting.objects.create(
            planning_year=self.py_2026,
            crop=self.product.crop,
            crop_season=crop_season,
            block=self.block,
            bed_start=4,
            bed_end=4,
            planned_bedfeet=50,
            planned_plant_date=mon_pick - timedelta(weeks=4),
            planned_first_harvest_date=mon_pick,
            planned_last_harvest_date=mon_pick + timedelta(weeks=2),
            planned_total_yield=Decimal("200.00"),
            status="growing",
        )
        HarvestEvent.objects.create(
            planting=planting,
            planned_date=mon_pick,
            planned_quantity=Decimal("12.00"),
            planned_units="bunch",
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk_view},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"data-weekly-supply-all-zero=", r.content)
        self.assertContains(r, "no harvest picks use dates in this ISO week")

    def test_weekly_order_save_persists_plan_row(self):
        wk = 22
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.post(
            url,
            {f"qty_{self.product.id}": "5"},
        )
        self.assertEqual(r.status_code, 302)
        mon = Week(2026, wk).monday()
        ev = SalesEvent.objects.filter(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=self.channel,
            product=self.product,
            sale_date=mon,
        ).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.planned_quantity, Decimal("5"))

    def test_weekly_order_live3_all_channel_demand_uses_week_date_window(self):
        """LIVE-3: all-channel PLAN totals use the same Mon–Sun as this page, not ISO week index alone."""
        wk = 20
        mon_2026 = Week(2026, wk).monday()
        mon_2025_same_iso = Week(2025, wk).monday()
        other = SalesChannel.objects.create(
            name="CSA Outlet",
            days_of_week=["Wednesday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("50.00"),
            is_csa=True,
            allocation_priority=4,
        )
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=other,
            product=self.product,
            sale_date=mon_2025_same_iso,
            planned_quantity=Decimal("999"),
        )
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=self.channel,
            product=self.product,
            sale_date=mon_2026,
            planned_quantity=Decimal("4"),
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "999")
        self.assertContains(r, "4.0")

    def test_plan_week_iso_counts_reflects_shadowing(self):
        """LIVE-3 helper: rollup plan rows drop when outlet owns the slice."""
        mon = Week(2026, 26).monday()
        cat, _ = SalesCategory.objects.update_or_create(
            name=SalesCategory.CategoryName.MARKETS,
            defaults={"allocation_priority": 10},
        )
        outlet = SalesChannel.objects.create(
            name="Outlet M",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("1.00"),
            is_csa=False,
            allocation_priority=2,
            category=cat,
        )
        rollup = SalesChannel.objects.create(
            name="Markets (annual plan)",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("1.00"),
            is_csa=False,
            allocation_priority=3,
            category=cat,
        )
        rows = [
            SalesEvent(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.py_2026,
                channel=rollup,
                product=self.product,
                sale_date=mon,
                planned_quantity=Decimal("99"),
            ),
            SalesEvent(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.py_2026,
                channel=outlet,
                product=self.product,
                sale_date=mon,
                planned_quantity=Decimal("1"),
            ),
        ]
        raw, visible = plan_week_iso_counts(rows, 26)
        self.assertEqual(raw, 2)
        self.assertEqual(visible, 1)

    def test_weekly_order_shows_supply_diagnostic_when_harvest_events_missing(self):
        wk = 27
        mon = Week(2026, wk).monday()
        cs = CropBySeason.objects.create(
            crop=self.product.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=21,
            rows_per_bed=4,
        )
        Planting.objects.create(
            planning_year=self.py_2026,
            crop=self.product.crop,
            crop_season=cs,
            block=self.block,
            bed_start=2,
            bed_end=2,
            planned_bedfeet=40,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=2),
            planned_total_yield=Decimal("40.00"),
            status="growing",
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"data-empty-reason=\"missing_generated_harvests\"", r.content)
        self.assertContains(r, "repair_planting_events --planning-year-id")
        self.assertIn(str(self.py_2026.id).encode(), r.content)

    def test_weekly_order_namesake_channel_fills_historical_and_warns(self):
        """LIVE-1: strict channel id has no ACTUAL rows; same-name channel supplies prior-year cells."""
        wk = 28
        mon_2024 = Week(2024, wk).monday()
        alt = SalesChannel.objects.create(
            name="Farmers Market",
            days_of_week=["Sunday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("1.00"),
            is_csa=False,
            allocation_priority=9,
        )
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            channel=alt,
            sale_date=mon_2024,
            product=self.product,
            actual_quantity=Decimal("7"),
            actual_revenue=Decimal("21.00"),
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "7.0")
        self.assertIn(b"data-empty-reason=\"historical_from_namesake_channel\"", r.content)

    def test_weekly_order_live3_diagnostic_when_no_plan_rows_for_iso_week(self):
        """LIVE-3: all-channel demand is zero because there are no PLAN rows for this week."""
        wk = 33
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-empty-reason="no_plan_rows_this_iso_week"', r.content)

    def test_weekly_order_live3_diagnostic_when_plan_rows_but_zero_demand(self):
        """LIVE-3: visible PLAN rows exist but every rolled-up quantity is zero."""
        wk = 34
        mon = Week(2026, wk).monday()
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=self.channel,
            product=self.product,
            sale_date=mon,
            planned_quantity=Decimal("0"),
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-empty-reason="plan_rows_zero_all_channel_demand"', r.content)

    def test_weekly_order_live2_products_limited_to_crop_plan_crops(self):
        """LIVE-2: weekly order rows only include crops with plantings for this planning year/week."""
        wk = 36
        mon = Week(2026, wk).monday()
        lettuce = CropInfo.objects.create(
            name="Lettuce",
            crop_type="Greens",
            botanical_family="Asteraceae",
            fresh_or_storage="fresh",
            harvest_unit="head",
            avg_unit_weight=Decimal("0.50"),
        )
        CropSalesFormat.objects.create(
            crop=lettuce,
            product_name="Lettuce Head",
            sale_price=Decimal("3.00"),
            sale_unit="head",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            sku="LET-HEAD",
            is_active=True,
        )
        cs = CropBySeason.objects.create(
            crop=self.product.crop,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=21,
            rows_per_bed=4,
        )
        Planting.objects.create(
            planning_year=self.py_2026,
            crop=self.product.crop,
            crop_season=cs,
            block=self.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=50,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=2),
            planned_total_yield=Decimal("50.00"),
            status="growing",
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Kale Bunch")
        self.assertNotContains(r, "Lettuce Head")

    def test_weekly_order_shows_partial_demand_shadow_hint(self):
        wk = 29
        mon = Week(2026, wk).monday()
        cat, _ = SalesCategory.objects.update_or_create(
            name=SalesCategory.CategoryName.MARKETS,
            defaults={"allocation_priority": 10},
        )
        self.channel.category = cat
        self.channel.save(update_fields=["category"])
        rollup = SalesChannel.objects.create(
            name="Markets (annual plan)",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("1.00"),
            is_csa=False,
            allocation_priority=5,
            category=cat,
        )
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=rollup,
            product=self.product,
            sale_date=mon,
            planned_quantity=Decimal("50"),
        )
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=self.channel,
            product=self.product,
            sale_date=mon,
            planned_quantity=Decimal("3"),
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"data-empty-reason=\"all_channel_demand_partially_shadowed\"", r.content)

    def test_weekly_order_warns_when_duplicate_channel_names_exist(self):
        wk = 30
        SalesChannel.objects.create(
            name=self.channel.name,
            days_of_week=["Sunday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("0.00"),
            is_csa=False,
            allocation_priority=99,
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'data-empty-reason="duplicate_channel_rows_same_name"', r.content)

    def test_weekly_order_live3_all_channel_demand_uses_week_date_window_boundaries(self):
        wk = 35
        mon = Week(2026, wk).monday()
        sun = mon + timedelta(days=6)
        next_mon = mon + timedelta(days=7)
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=self.channel,
            product=self.product,
            sale_date=sun,
            planned_quantity=Decimal("9"),
        )
        SalesEvent.objects.create(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.py_2026,
            channel=self.channel,
            product=self.product,
            sale_date=next_mon,
            planned_quantity=Decimal("40"),
        )
        url = reverse(
            "sales:weekly_channel_order",
            kwargs={"channel_id": self.channel.id, "week": wk},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, ">9.0<", html=False)
        self.assertNotContains(r, ">49.0<", html=False)


class MarketSalesEntryLive2Tests(TestCase):
    """LIVE-2: market entry without pack list limits products to crops in the active crop plan."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from reference.models import Block

        User = get_user_model()
        cls.staff = User.objects.create_user("market_live2_staff", password="pw", is_staff=True)
        cls.channel = SalesChannel.objects.create(
            name="Farmers Market",
            days_of_week=["Saturday"],
            start_week=1,
            end_week=52,
            weekly_target=Decimal("400.00"),
            is_csa=False,
            allocation_priority=1,
        )
        cls.py = PlanningYear.objects.create(year=2026, status="active")
        kale = CropInfo.objects.create(
            name="Kale",
            crop_type="Greens",
            botanical_family="Brassicaceae",
            fresh_or_storage="fresh",
            harvest_unit="bunch",
            avg_unit_weight=Decimal("0.40"),
        )
        cls.kale_product = CropSalesFormat.objects.create(
            crop=kale,
            product_name="Kale Bunch",
            sale_price=Decimal("4.00"),
            sale_unit="bunch",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            sku="KAL-BUN-MKT",
            is_active=True,
        )
        lettuce = CropInfo.objects.create(
            name="Lettuce",
            crop_type="Greens",
            botanical_family="Asteraceae",
            fresh_or_storage="fresh",
            harvest_unit="head",
            avg_unit_weight=Decimal("0.50"),
        )
        CropSalesFormat.objects.create(
            crop=lettuce,
            product_name="Lettuce Head",
            sale_price=Decimal("3.00"),
            sale_unit="head",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            sku="LET-HEAD-MKT",
            is_active=True,
        )
        block = Block.objects.create(
            name="B1",
            block_type="field",
            num_beds=8,
            bed_width_feet=Decimal("4.0"),
            bedfeet_per_bed=100,
            walk_route_order=1,
        )
        mon = Week(2026, 22).monday()
        cs = CropBySeason.objects.create(
            crop=kale,
            block_type="field",
            field_week_start=1,
            field_week_end=52,
            total_yield_per_bedfoot=Decimal("1.00"),
            harvest_weeks=4,
            dtm_days=30,
            rows_per_bed=4,
        )
        Planting.objects.create(
            planning_year=cls.py,
            crop=kale,
            crop_season=cs,
            block=block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=50,
            planned_plant_date=mon - timedelta(weeks=2),
            planned_first_harvest_date=mon,
            planned_last_harvest_date=mon + timedelta(weeks=2),
            planned_total_yield=Decimal("50.00"),
            status="growing",
        )

    def setUp(self):
        self.client.login(username="market_live2_staff", password="pw")
        session = self.client.session
        session["planning_year_id"] = self.py.id
        session.save()

    def test_market_entry_without_pack_limits_to_crop_plan_crops(self):
        sale_date = Week(2026, 22).monday()
        url = reverse("sales:market_entry")
        r = self.client.get(
            url,
            {"channel": str(self.channel.id), "date": sale_date.isoformat()},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Kale Bunch")
        self.assertNotContains(r, "Lettuce Head")

    def test_market_list_print_without_pack_limits_to_crop_plan_crops(self):
        week = 22
        url = reverse(
            "sales:market_list_print",
            kwargs={"channel_id": self.channel.id, "week": week},
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Kale Bunch")
        self.assertNotContains(r, "Lettuce Head")
