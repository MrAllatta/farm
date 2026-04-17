from django.contrib import admin
from .models import PackBatch, PackBatchComponent


class PackBatchComponentInline(admin.TabularInline):
    model = PackBatchComponent
    extra = 1
    fields = (
        "source_crop",
        "source_product",
        "consumed_quantity",
        "consumed_unit",
        "component_percent",
        "inventory_ledger_entry",
    )


@admin.register(PackBatch)
class PackBatchAdmin(admin.ModelAdmin):
    list_display = ("pack_date", "product", "recipe", "packed_quantity", "packed_unit")
    list_filter = ("pack_date", "product")
    search_fields = ("product__product_name", "notes")
    inlines = [PackBatchComponentInline]

