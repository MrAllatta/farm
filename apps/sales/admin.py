from django.contrib import admin
from .models import SalesEvent, QuickSalesEntry


@admin.register(SalesEvent)
class SalesEventAdmin(admin.ModelAdmin):
    list_display = (
        "sale_date",
        "entry_kind",
        "planning_year",
        "channel",
        "sales_category",
        "product",
        "pack_batch",
        "planned_quantity",
        "actual_quantity",
        "actual_revenue",
    )
    list_filter = ("entry_kind", "planning_year", "channel", "sales_category")
    search_fields = (
        "channel__name",
        "sales_category__name",
        "product__product_name",
        "product__crop__name",
    )
    raw_id_fields = ("planning_year", "channel", "sales_category", "product", "pack_batch")


@admin.register(QuickSalesEntry)
class QuickSalesEntryAdmin(admin.ModelAdmin):
    list_display = ("sale_date", "channel", "total_cash", "total_card")
    list_filter = ("channel",)
    search_fields = ("channel__name", "notes")
