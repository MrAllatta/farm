"""reference/admin.py"""

from django.contrib import admin
from .models import (
    Block,
    CropBySeason,
    CropInfo,
    CropSalesFormat,
    CropSalesFormatYear,
    ProductRecipe,
    ProductRecipeComponent,
    SalesCategory,
    SalesChannel,
    SalesPlanBucket,
    Variety,
)


class CropBySeasonInline(admin.TabularInline):
    model = CropBySeason
    extra = 0
    fields = [
        "block_type",
        "field_week_start",
        "field_week_end",
        "total_yield_per_bedfoot",
        "harvest_weeks",
        "dtm_days",
        "rows_per_bed",
        "ds_seed_rate",
        "tp_inrow_spacing",
        "irrigation",
    ]


class CropSalesFormatInline(admin.TabularInline):
    model = CropSalesFormat
    extra = 1
    fields = [
        "product_name",
        "sale_price",
        "sale_unit",
        "harvest_qty_per_sale_unit",
        "is_active",
    ]
    # sale_price / is_active are denormalized from CropSalesFormatYear (latest planning year).
    readonly_fields = ["sale_price", "is_active"]


@admin.register(CropSalesFormatYear)
class CropSalesFormatYearAdmin(admin.ModelAdmin):
    list_display = ["product", "planning_year", "sale_price", "is_active"]
    list_filter = ["planning_year", "is_active"]
    search_fields = ["product__product_name"]
    raw_id_fields = ["product", "planning_year"]


@admin.register(Variety)
class VarietyAdmin(admin.ModelAdmin):
    list_display = ["name", "crop", "supplier", "catalog_number", "source_url"]
    list_filter = ["crop__crop_type"]
    search_fields = ["name", "supplier", "catalog_number", "crop__name"]
    raw_id_fields = ["crop"]


@admin.register(CropInfo)
class CropInfoAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "crop_type",
        "botanical_family",
        "fresh_or_storage",
        "can_hold_in_field",
        "harvest_unit",
        "nursery_weeks",
    ]
    list_filter = [
        "crop_type",
        "botanical_family",
        "fresh_or_storage",
        "can_hold_in_field",
        "propagation_type",
        "is_perennial",
    ]
    search_fields = ["name", "crop_type", "botanical_family"]
    inlines = [CropBySeasonInline, CropSalesFormatInline]

    fieldsets = (
        (
            "Reference",
            {
                "fields": (
                    "name",
                    "crop_type",
                    "botanical_family",
                    "propagation_type",
                    "is_perennial",
                )
            },
        ),
        (
            "Harvest",
            {
                "fields": (
                    "fresh_or_storage",
                    "storage_weeks",
                    "can_hold_in_field",
                    "harvest_unit",
                    "avg_unit_weight",
                    "units_per_bin",
                    "harvest_bin",
                    "harvest_tools",
                    "harvest_rate_per_hour",
                )
            },
        ),
        (
            "Nursery",
            {
                "fields": (
                    "nursery_weeks",
                    "weeks_until_pot_up",
                    "pot_up_tray_size",
                    "seeded_tray_size",
                    "seeds_per_cell",
                    "thinned_plants",
                    "seeds_per_ounce",
                )
            },
        ),
    )


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "block_type",
        "num_beds",
        "bedfeet_per_bed",
        "total_bedfeet",
        "walk_route_order",
    ]
    list_filter = ["block_type"]
    list_editable = ["walk_route_order"]
    ordering = ["walk_route_order", "name"]


@admin.register(SalesChannel)
class SalesChannelAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "category",
        "plan_bucket",
        "days_of_week",
        "start_week",
        "end_week",
        "num_weeks",
        "weekly_target",
        "annual_target",
        "allocation_priority",
    ]
    list_editable = ["weekly_target", "allocation_priority"]


@admin.register(SalesCategory)
class SalesCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "allocation_priority"]
    list_editable = ["allocation_priority"]
    ordering = ["allocation_priority", "name"]


@admin.register(SalesPlanBucket)
class SalesPlanBucketAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "category",
        "start_week",
        "end_week",
        "weekly_target",
        "annual_target",
        "allocation_priority",
        "is_active",
    ]
    list_filter = ["category", "is_active"]
    list_editable = ["weekly_target", "allocation_priority", "is_active"]


class ProductRecipeComponentInline(admin.TabularInline):
    model = ProductRecipeComponent
    extra = 1
    fields = [
        "source_crop",
        "source_product",
        "component_quantity",
        "component_unit",
        "component_percent",
        "sort_order",
    ]


@admin.register(ProductRecipe)
class ProductRecipeAdmin(admin.ModelAdmin):
    list_display = ("name", "product", "is_active", "effective_start", "effective_end")
    list_filter = ("is_active",)
    search_fields = ("name", "product__product_name")
    inlines = [ProductRecipeComponentInline]
