from datetime import date
from decimal import Decimal

from django.test import TestCase

from reference.models import CropInfo, CropSalesFormat, SalesChannel
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
