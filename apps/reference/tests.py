from decimal import Decimal

from django.test import TestCase

from reference.models import Block, BlockType, CropBySeason, CropInfo, SalesChannel


class ReferenceModelTests(TestCase):
    def test_block_geometry_properties_are_derived_from_beds_and_width(self):
        block = Block.objects.create(
            name="Field 1",
            block_type=BlockType.FIELD,
            num_beds=10,
            bed_width_feet=Decimal("3.5"),
            bedfeet_per_bed=100,
        )

        self.assertEqual(block.total_bedfeet, 1000)
        self.assertEqual(block.square_feet, Decimal("3500.0"))

    def test_crop_by_season_derived_metrics_handle_rounding_and_zero_harvest_weeks(self):
        crop = CropInfo.objects.create(
            name="Carrot",
            crop_type="Vegetables",
            botanical_family="Apiaceae",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
        )
        profile = CropBySeason.objects.create(
            crop=crop,
            block_type=BlockType.FIELD,
            field_week_start=10,
            field_week_end=40,
            total_yield_per_bedfoot=Decimal("1.50"),
            harvest_weeks=4,
            dtm_days=16,
            rows_per_bed=3,
        )
        zero_harvest_profile = CropBySeason.objects.create(
            crop=CropInfo.objects.create(
                name="Spinach",
                crop_type="Greens",
                botanical_family="Amaranthaceae",
                fresh_or_storage="fresh",
                harvest_unit="pounds",
                avg_unit_weight=Decimal("1.00"),
            ),
            block_type=BlockType.HIGH_TUNNEL,
            field_week_start=8,
            field_week_end=30,
            total_yield_per_bedfoot=Decimal("2.00"),
            harvest_weeks=0,
            dtm_days=15,
            rows_per_bed=4,
        )

        self.assertEqual(profile.wtm_weeks, 3)
        self.assertEqual(profile.weekly_yield_per_bedfoot, Decimal("0.375"))
        self.assertEqual(zero_harvest_profile.weekly_yield_per_bedfoot, Decimal("0"))

    def test_sales_channel_wraparound_target_uses_cross_year_week_count(self):
        channel = SalesChannel.objects.create(
            name="Winter CSA",
            days_of_week=["Tuesday"],
            start_week=48,
            end_week=4,
            weekly_target=Decimal("125.00"),
            is_csa=True,
            allocation_priority=1,
        )

        self.assertEqual(channel.num_weeks, 9)
        self.assertEqual(channel.annual_target, Decimal("1125.00"))
