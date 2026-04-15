from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from operations.models import InventoryLedger
from planning.models import PlanningYear
from reference.models import CropInfo


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
