from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from reference.models import (
    Block,
    BlockType,
    CropBySeason,
    CropInfo,
    CropSalesFormat,
    ProductRecipe,
    ProductRecipeComponent,
    SalesChannel,
)


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


class ProductRecipeInvariantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.crop = CropInfo.objects.create(
            name="Mix Lettuce",
            crop_type="Greens",
            botanical_family="Asteraceae",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
        )
        cls.product = CropSalesFormat.objects.create(
            crop=cls.crop,
            product_name="Salad Mix Bag",
            sale_price=Decimal("6.00"),
            sale_unit="bag",
            harvest_qty_per_sale_unit=Decimal("1.00"),
            is_active=True,
        )
        cls.other_crop = CropInfo.objects.create(
            name="Baby Kale",
            crop_type="Greens",
            botanical_family="Brassicaceae",
            fresh_or_storage="fresh",
            harvest_unit="pounds",
            avg_unit_weight=Decimal("1.00"),
        )

    def test_recipe_percent_components_must_sum_to_100(self):
        recipe = ProductRecipe.objects.create(product=self.product, name="Default")
        ProductRecipeComponent.objects.create(
            recipe=recipe,
            source_crop=self.crop,
            component_quantity=Decimal("1.00"),
            component_unit="pounds",
            component_percent=Decimal("60.00"),
        )
        ProductRecipeComponent.objects.create(
            recipe=recipe,
            source_crop=self.other_crop,
            component_quantity=Decimal("1.00"),
            component_unit="pounds",
            component_percent=Decimal("30.00"),
        )
        with self.assertRaises(ValidationError):
            recipe.validate_component_totals()

    def test_component_requires_exactly_one_source(self):
        recipe = ProductRecipe.objects.create(product=self.product, name="Default")
        component = ProductRecipeComponent(
            recipe=recipe,
            source_crop=self.crop,
            source_product=self.product,
            component_quantity=Decimal("1.00"),
            component_unit="pounds",
        )
        with self.assertRaises(ValidationError):
            component.clean()
