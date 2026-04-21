"""planning.admin"""

from django.contrib import admin
from .models import HarvestEvent, NurseryEvent, PlanningYear, Planting, SeedOrder


class NurseryEventInline(admin.TabularInline):
    model = NurseryEvent
    extra = 0
    fields = [
        "event_type",
        "planned_date",
        "planned_tray_count",
        "planned_tray_size",
        "actual_date",
        "actual_tray_count",
        "notes",
    ]
    readonly_fields = ["event_type", "planned_date", "planned_tray_count", "planned_tray_size"]


@admin.register(Planting)
class PlantingAdmin(admin.ModelAdmin):
    list_display = [
        "crop",
        "variety",
        "block",
        "bed_range",
        "planned_bedfeet",
        "planned_plant_week",
        "planned_harvest_range",
        "status",
    ]
    list_filter = [
        "status",
        "planning_year",
        "block__block_type",
        "crop__crop_type",
        "crop__botanical_family",
    ]
    search_fields = ["crop__name", "variety", "block__name", "notes"]
    raw_id_fields = ["crop", "crop_season", "revision_of", "variety_obj"]
    inlines = [NurseryEventInline]

    fieldsets = (
        (
            "Crop & Location",
            {
                "fields": (
                    "planning_year",
                    "crop",
                    "crop_season",
                    "variety",
                    "variety_obj",
                    "block",
                    "bed_start",
                    "bed_end",
                )
            },
        ),
        (
            "Plan",
            {
                "fields": (
                    "planned_bedfeet",
                    "planned_plant_date",
                    "planned_first_harvest_date",
                    "planned_last_harvest_date",
                    "planned_total_yield",
                )
            },
        ),
        (
            "Actual",
            {
                "fields": (
                    "actual_bedfeet",
                    "actual_plant_date",
                    "actual_first_harvest_date",
                    "actual_last_harvest_date",
                    "actual_total_yield",
                )
            },
        ),
        ("Status", {"fields": ("status", "revision_of", "succession_group", "notes")}),
    )

    def bed_range(self, obj):
        return f"{obj.bed_start}-{obj.bed_end}"

    bed_range.short_description = "Beds"

    def planned_plant_week(self, obj):
        return f"Wk {obj.planned_plant_date.isocalendar()[1]}"

    planned_plant_week.short_description = "Plant Wk"

    def planned_harvest_range(self, obj):
        start = obj.planned_first_harvest_date.isocalendar()[1]
        end = obj.planned_last_harvest_date.isocalendar()[1]
        return f"Wk {start}-{end}"

    planned_harvest_range.short_description = "Harvest"


@admin.register(SeedOrder)
class SeedOrderAdmin(admin.ModelAdmin):
    list_display = ["variety", "planning_year", "planned_quantity", "unit"]
    list_filter = ["planning_year"]
    raw_id_fields = ["variety", "planning_year"]


@admin.register(PlanningYear)
class PlanningYearAdmin(admin.ModelAdmin):
    model = PlanningYear
    extra = 0
    fields = [
        "year",
        "status",
        "overplant_factor",
    ]
