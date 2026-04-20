from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from isoweek import Week

from operations.models import FieldWalkNote, InventoryLedger, PackBatch, PackBatchComponent
from planning.models import HarvestEvent, PlanningYear, Planting
from reference.models import Block, CropBySeason, CropInfo, CropSalesFormat


class InventoryLedgerGateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        PlanningYear.objects.create(year=2026, status="active")
        cls.storage_crop = CropInfo.objects.create(
            name="Storage Carrot",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=16,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )

    def _post_txn(self, event_type, quantity):
        response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": self.storage_crop.pk,
                "event_type": event_type,
                "quantity": quantity,
                "notes": f"{event_type} test",
            },
        )
        self.assertEqual(response.status_code, 302)
        return InventoryLedger.objects.filter(crop=self.storage_crop).order_by("-id").first()

    def setUp(self):
        user_model = get_user_model()
        self.staff_user = user_model.objects.create_user(
            username="ops_staff",
            email="ops@example.com",
            password="test-pass-123",
            is_staff=True,
        )
        self.client.force_login(self.staff_user)

    def test_inventory_transaction_sign_conventions_and_running_balance(self):
        first = self._post_txn("return_in", "10")
        self.assertEqual(first.quantity, Decimal("10"))
        self.assertEqual(first.running_balance, Decimal("10"))

        second = self._post_txn("sale_out", "3.50")
        self.assertEqual(second.quantity, Decimal("-3.50"))
        self.assertEqual(second.running_balance, Decimal("6.50"))

        third = self._post_txn("waste_out", "1.25")
        self.assertEqual(third.quantity, Decimal("-1.25"))
        self.assertEqual(third.running_balance, Decimal("5.25"))

        fourth = self._post_txn("quality_check", "99")
        self.assertEqual(fourth.quantity, Decimal("0"))
        self.assertEqual(fourth.running_balance, Decimal("5.25"))

    def test_inventory_transaction_allows_negative_balance_with_explicit_signal(self):
        self._post_txn("return_in", "2")
        response = self.client.post(
            reverse("operations:inventory_add"),
            {
                "crop": self.storage_crop.pk,
                "event_type": "sale_out",
                "quantity": "5",
                "notes": "forced negative balance",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "balance would go negative")

        last = InventoryLedger.objects.filter(crop=self.storage_crop).order_by("-id").first()
        self.assertEqual(last.quantity, Decimal("-5"))
        self.assertEqual(last.running_balance, Decimal("-3"))

    def test_inventory_model_save_autocalculates_balance_when_running_balance_not_given(self):
        first = InventoryLedger.objects.create(
            crop=self.storage_crop,
            event_date=date(2026, 1, 1),
            event_type="harvest_in",
            quantity=Decimal("4.00"),
            running_balance=Decimal("0"),
        )
        second = InventoryLedger.objects.create(
            crop=self.storage_crop,
            event_date=date(2026, 1, 2),
            event_type="sale_out",
            quantity=Decimal("-1.50"),
            running_balance=Decimal("0"),
        )

        self.assertEqual(first.running_balance, Decimal("4.00"))
        self.assertEqual(second.running_balance, Decimal("2.50"))


class OperationsSliceOneTemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = PlanningYear.objects.create(year=2026, status="active")
        cls.storage_crop = CropInfo.objects.create(
            name="Storage Beet",
            crop_type="Vegetables",
            botanical_family="Amaranthaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=10,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        cls.block = Block.objects.create(
            name="A",
            block_type="field",
            num_beds=4,
            bed_width_feet="2.5",
            bedfeet_per_bed=50,
            walk_route_order=1,
        )
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.storage_crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot="1.00",
            harvest_weeks=4,
            dtm_days=50,
            rows_per_bed=2,
            ds_seed_rate=20,
        )
        cls.planting = Planting.objects.create(
            planning_year=cls.year,
            crop=cls.storage_crop,
            crop_season=cls.crop_season,
            block=cls.block,
            bed_start=1,
            bed_end=2,
            planned_bedfeet=100,
            planned_plant_date=date(2026, 4, 1),
            planned_first_harvest_date=date(2026, 6, 1),
            planned_last_harvest_date=date(2026, 6, 22),
            planned_total_yield="100.00",
            status="growing",
        )
        cls.harvest_event = HarvestEvent.objects.create(
            planting=cls.planting,
            planned_date=date(2026, 6, 8),
            planned_quantity="25.00",
            planned_units="pounds",
            actual_quantity="20.00",
            actual_units="pounds",
            notes="good quality",
        )
        FieldWalkNote.objects.create(
            planting=cls.planting,
            walk_date=date(2026, 5, 20),
            condition="good",
            yield_adjust_pct=95,
            notes="light pest pressure",
        )

    def test_field_walk_route_renders_dedicated_template_with_empty_state(self):
        self.planting.delete()
        response = self.client.get(reverse("operations:field_walk_current"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "operations/field_walk.html")
        self.assertContains(response, "Field Walk")
        self.assertContains(response, "No active plantings for field walk")
        self.assertEqual(response.context["total_plantings"], 0)

    def test_inventory_route_renders_dedicated_template_with_empty_state(self):
        response = self.client.get(reverse("operations:inventory"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "operations/inventory.html")
        self.assertContains(response, "Storage Inventory")
        self.assertContains(response, "No storage inventory yet")
        self.assertEqual(response.context["total_items"], 0)
        self.assertEqual(response.context["critical_count"], 0)
        self.assertEqual(response.context["warning_count"], 0)

    def test_inventory_route_renders_items_when_ledger_has_balance(self):
        InventoryLedger.objects.create(
            crop=self.storage_crop,
            event_date=date(2026, 1, 5),
            event_type="return_in",
            quantity=Decimal("8.00"),
            running_balance=Decimal("8.00"),
            storage_location="Root cellar",
        )
        response = self.client.get(reverse("operations:inventory"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Storage Beet")
        self.assertContains(response, "Root cellar")
        self.assertEqual(response.context["total_items"], 1)

    def test_inventory_add_route_renders_transaction_form_template(self):
        response = self.client.get(reverse("operations:inventory_add"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "operations/inventory_transaction.html")
        self.assertContains(response, "Record Inventory Transaction")

    def test_detail_routes_render_dedicated_templates_with_context(self):
        planting_harvest_response = self.client.get(
            reverse("operations:harvest_entry", kwargs={"pk": self.planting.pk})
        )
        self.assertEqual(planting_harvest_response.status_code, 200)
        self.assertTemplateUsed(planting_harvest_response, "operations/planting_harvest_entry.html")
        self.assertContains(planting_harvest_response, "Storage Beet")
        self.assertContains(planting_harvest_response, "Harvest Events")

        field_walk_note_response = self.client.get(
            reverse("operations:field_walk", kwargs={"pk": self.planting.pk})
        )
        self.assertEqual(field_walk_note_response.status_code, 200)
        self.assertTemplateUsed(field_walk_note_response, "operations/field_walk_note.html")
        self.assertContains(field_walk_note_response, "light pest pressure")
        self.assertContains(field_walk_note_response, "Add Field Walk Note")

        harvest_in_response = self.client.get(
            reverse("operations:inventory_harvest_in", kwargs={"harvest_event_id": self.harvest_event.pk})
        )
        self.assertEqual(harvest_in_response.status_code, 200)
        self.assertTemplateUsed(harvest_in_response, "operations/inventory_harvest_in.html")
        self.assertContains(harvest_in_response, "Storage Beet")

    def test_detail_routes_return_404_for_unknown_records(self):
        self.assertEqual(
            self.client.get(reverse("operations:harvest_entry", kwargs={"pk": 999999})).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("operations:field_walk", kwargs={"pk": 999999})).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("operations:inventory_harvest_in", kwargs={"harvest_event_id": 999999})
            ).status_code,
            404,
        )


class FieldWalkNoteViewPostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = PlanningYear.objects.create(year=2026, status="active")
        cls.other_year = PlanningYear.objects.create(year=2025, status="complete")
        cls.crop = CropInfo.objects.create(
            name="Field Walk Crop",
            crop_type="Vegetables",
            botanical_family="Brassicaceae",
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
        cls.block = Block.objects.create(
            name="B",
            block_type="field",
            num_beds=4,
            bed_width_feet="2.5",
            bedfeet_per_bed=50,
            walk_route_order=2,
        )
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot="1.00",
            harvest_weeks=4,
            dtm_days=50,
            rows_per_bed=2,
            ds_seed_rate=20,
        )
        cls.planting = Planting.objects.create(
            planning_year=cls.year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            block=cls.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=50,
            planned_plant_date=date(2026, 4, 1),
            planned_first_harvest_date=date(2026, 6, 1),
            planned_last_harvest_date=date(2026, 6, 22),
            planned_total_yield="50.00",
            status="growing",
            notes="",
        )
        cls.other_year_planting = Planting.objects.create(
            planning_year=cls.other_year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            block=cls.block,
            bed_start=2,
            bed_end=2,
            planned_bedfeet=40,
            planned_plant_date=date(2025, 4, 1),
            planned_first_harvest_date=date(2025, 6, 1),
            planned_last_harvest_date=date(2025, 6, 22),
            planned_total_yield="40.00",
            status="growing",
            notes="",
        )

    def setUp(self):
        user_model = get_user_model()
        self.staff_user = user_model.objects.create_user(
            username="field_walk_staff",
            email="fw@example.com",
            password="test-pass-123",
            is_staff=True,
        )
        self.non_staff_user = user_model.objects.create_user(
            username="field_walk_user",
            email="fwu@example.com",
            password="test-pass-123",
            is_staff=False,
        )

    def test_field_walk_note_post_staff_creates_note(self):
        self.client.force_login(self.staff_user)
        before = FieldWalkNote.objects.filter(planting=self.planting).count()
        response = self.client.post(
            reverse("operations:field_walk", kwargs={"pk": self.planting.pk}),
            {
                "condition": "fair",
                "yield_adjust": "90",
                "notes": "single-route note",
                "adj_harvest": "24",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            FieldWalkNote.objects.filter(planting=self.planting).count(),
            before + 1,
        )
        note = FieldWalkNote.objects.filter(planting=self.planting).order_by("-id").first()
        self.assertEqual(note.condition, "fair")
        self.assertEqual(note.yield_adjust_pct, 90)
        self.assertEqual(note.notes, "single-route note")
        self.assertIsNotNone(note.adjusted_first_harvest_date)

    def test_field_walk_note_post_failed_updates_planting(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("operations:field_walk", kwargs={"pk": self.planting.pk}),
            {
                "condition": "failed",
                "yield_adjust": "100",
                "notes": "crop loss",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.planting.refresh_from_db()
        self.assertEqual(self.planting.status, "failed")
        self.assertIn("crop loss", self.planting.notes)

    def test_field_walk_note_post_missing_condition_shows_warning(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("operations:field_walk", kwargs={"pk": self.planting.pk}),
            {"notes": "no condition selected"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a condition")

    def test_field_walk_note_post_anonymous_redirects_to_login(self):
        response = self.client.post(
            reverse("operations:field_walk", kwargs={"pk": self.planting.pk}),
            {"condition": "good"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_field_walk_note_post_non_staff_forbidden(self):
        self.client.force_login(self.non_staff_user)
        response = self.client.post(
            reverse("operations:field_walk", kwargs={"pk": self.planting.pk}),
            {"condition": "good"},
        )
        self.assertEqual(response.status_code, 403)

    def test_field_walk_note_get_outside_current_planning_year_returns_404(self):
        response = self.client.get(
            reverse("operations:field_walk", kwargs={"pk": self.other_year_planting.pk})
        )
        self.assertEqual(response.status_code, 404)


class WeeklyHarvestEntryViewWeekRouteTests(TestCase):
    """Batch harvest entry: explicit week URL and POST contract."""

    @classmethod
    def setUpTestData(cls):
        cls.year = PlanningYear.objects.create(year=2026, status="active")
        cls.crop = CropInfo.objects.create(
            name="Harvest Week Crop",
            crop_type="Vegetables",
            botanical_family="Brassicaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="fresh",
            storage_weeks=0,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            units_per_bin=5,
            harvest_bin="bin",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        cls.block = Block.objects.create(
            name="HBlock",
            block_type="field",
            num_beds=4,
            bed_width_feet="2.5",
            bedfeet_per_bed=50,
            walk_route_order=1,
        )
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot="1.00",
            harvest_weeks=4,
            dtm_days=50,
            rows_per_bed=2,
            ds_seed_rate=20,
        )
        cls.planting = Planting.objects.create(
            planning_year=cls.year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            block=cls.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=50,
            planned_plant_date=date(2026, 4, 1),
            planned_first_harvest_date=date(2026, 6, 1),
            planned_last_harvest_date=date(2026, 6, 22),
            planned_total_yield="50.00",
            status="growing",
        )
        cls.target_week = 23
        cls.week_mid = Week(2026, cls.target_week).monday() + timedelta(days=2)
        cls.harvest_event = HarvestEvent.objects.create(
            planting=cls.planting,
            planned_date=cls.week_mid,
            planned_quantity="25.00",
            planned_units="pounds",
        )

    def setUp(self):
        user_model = get_user_model()
        self.staff = user_model.objects.create_user(
            username="harvest_staff",
            email="h@example.com",
            password="test-pass-123",
            is_staff=True,
        )

    def test_week_route_shows_event_and_week_navigation(self):
        self.client.force_login(self.staff)
        url = reverse("operations:harvest_entry_week", kwargs={"week": self.target_week})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["week_num"], self.target_week)
        self.assertContains(response, "Harvest Week Crop")
        self.assertEqual(response.context["prev_week"], 22)
        self.assertEqual(response.context["next_week"], 24)

    def test_week_route_post_records_bins_and_redirects_back_to_same_week(self):
        self.client.force_login(self.staff)
        url = reverse("operations:harvest_entry_week", kwargs={"week": self.target_week})
        response = self.client.post(
            url,
            {f"bins_{self.harvest_event.pk}": "2", f"notes_{self.harvest_event.pk}": "ok"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/operations/harvest/week/{self.target_week}/", response["Location"])
        self.harvest_event.refresh_from_db()
        self.assertEqual(self.harvest_event.actual_bins, 2.0)
        self.assertEqual(self.harvest_event.notes, "ok")


class InventoryDashboardViewStatusOrderingTests(TestCase):
    """InventoryDashboardView: summary counts and critical-first ordering."""

    @classmethod
    def setUpTestData(cls):
        PlanningYear.objects.create(year=2026, status="active")
        today = date.today()
        cls.critical_crop = CropInfo.objects.create(
            name="Crit Kale",
            crop_type="Vegetables",
            botanical_family="Brassicaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=8,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        cls.good_crop = CropInfo.objects.create(
            name="Good Potato",
            crop_type="Vegetables",
            botanical_family="Solanaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=20,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        InventoryLedger.objects.create(
            crop=cls.good_crop,
            event_date=today - timedelta(days=30),
            event_type="return_in",
            quantity=Decimal("100.00"),
            running_balance=Decimal("100.00"),
            expiry_date=today + timedelta(weeks=20),
            storage_location="Cellar A",
        )
        InventoryLedger.objects.create(
            crop=cls.critical_crop,
            event_date=today - timedelta(days=2),
            event_type="return_in",
            quantity=Decimal("12.00"),
            running_balance=Decimal("12.00"),
            expiry_date=today + timedelta(days=12),
            storage_location="Cellar C",
        )

    def test_dashboard_orders_critical_before_good_and_counts_critical(self):
        response = self.client.get(reverse("operations:inventory"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["critical_count"], 1)
        self.assertEqual(response.context["warning_count"], 0)
        items = response.context["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["status"], "critical")
        self.assertEqual(items[0]["crop"].name, "Crit Kale")
        self.assertEqual(items[1]["status"], "good")


class InventoryHarvestInViewTests(TestCase):
    """DESIGN_GATE: harvest-to-inventory POST intake is not specified; GET surface is covered."""

    @classmethod
    def setUpTestData(cls):
        cls.year = PlanningYear.objects.create(year=2026, status="active")
        cls.crop = CropInfo.objects.create(
            name="Harvest In Crop",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=8,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        cls.block = Block.objects.create(
            name="InvBlock",
            block_type="field",
            num_beds=2,
            bed_width_feet="2.5",
            bedfeet_per_bed=50,
            walk_route_order=1,
        )
        cls.crop_season = CropBySeason.objects.create(
            crop=cls.crop,
            block_type="field",
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot="1.00",
            harvest_weeks=4,
            dtm_days=50,
            rows_per_bed=2,
            ds_seed_rate=20,
        )
        cls.planting = Planting.objects.create(
            planning_year=cls.year,
            crop=cls.crop,
            crop_season=cls.crop_season,
            block=cls.block,
            bed_start=1,
            bed_end=1,
            planned_bedfeet=50,
            planned_plant_date=date(2026, 4, 1),
            planned_first_harvest_date=date(2026, 6, 1),
            planned_last_harvest_date=date(2026, 6, 22),
            planned_total_yield="50.00",
            status="growing",
        )
        cls.harvest_event = HarvestEvent.objects.create(
            planting=cls.planting,
            planned_date=date(2026, 6, 8),
            planned_quantity="25.00",
            planned_units="pounds",
        )

    def test_get_shows_harvest_context(self):
        response = self.client.get(
            reverse("operations:inventory_harvest_in", kwargs={"harvest_event_id": self.harvest_event.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Harvest In Crop")
        self.assertEqual(response.context["harvest_event"], self.harvest_event)
        self.assertEqual(response.context["current_balance"], Decimal("0"))

    def test_post_returns_method_not_allowed_until_intake_design_gate(self):
        """DESIGN_GATE: written harvest-to-inventory intake policy + form not implemented."""
        response = self.client.post(
            reverse("operations:inventory_harvest_in", kwargs={"harvest_event_id": self.harvest_event.pk}),
            {"quantity": "5"},
        )
        self.assertEqual(response.status_code, 405)


class MixPackBatchInventoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.crop = CropInfo.objects.create(
            name="Mix Component Crop",
            crop_type="Greens",
            botanical_family="Asteraceae",
            propagation_type="seed",
            is_perennial=False,
            fresh_or_storage="storage",
            storage_weeks=2,
            harvest_unit="pounds",
            avg_unit_weight="1.00",
            nursery_weeks=0,
            weeks_until_pot_up=0,
            seeds_per_cell=1,
            thinned_plants=0,
        )
        cls.product = CropSalesFormat.objects.create(
            crop=cls.crop,
            product_name="Salad Mix Bag",
            sale_price="6.00",
            sale_unit="bag",
            harvest_qty_per_sale_unit="1.00",
            is_active=True,
        )

    def test_post_component_consumption_creates_negative_inventory_drawdown(self):
        InventoryLedger.objects.create(
            crop=self.crop,
            event_date=date(2026, 1, 1),
            event_type="harvest_in",
            quantity=Decimal("25.00"),
            running_balance=Decimal("25.00"),
        )
        batch = PackBatch.objects.create(
            product=self.product,
            packed_quantity=Decimal("10.00"),
            packed_unit="bag",
            pack_date=date(2026, 1, 2),
        )
        PackBatchComponent.objects.create(
            pack_batch=batch,
            source_crop=self.crop,
            consumed_quantity=Decimal("8.00"),
            consumed_unit="pounds",
            component_percent=Decimal("100.00"),
        )

        entries = batch.post_component_consumption()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.quantity, Decimal("-8.00"))
        self.assertEqual(entry.running_balance, Decimal("17.00"))
