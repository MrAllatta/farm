from datetime import date
from decimal import Decimal

from django.test import TestCase

from operations.models import InventoryLedger, PackBatch
from reference.models import CropInfo, CropSalesFormat, SalesChannel
from planning.models import PlanningYear
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
