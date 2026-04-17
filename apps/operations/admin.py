from django.contrib import admin
from django.core.exceptions import ValidationError

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
    actions = ("post_inventory_consumption",)

    @admin.action(description="Post inventory consumption (crop-backed components)")
    def post_inventory_consumption(self, request, queryset):
        posted = 0
        errors = []
        for batch in queryset:
            try:
                batch.post_component_consumption()
                posted += 1
            except ValidationError as e:
                errors.append(f"PackBatch {batch.pk}: {e}")
        if posted:
            self.message_user(request, f"Posted consumption for {posted} batch(es).")
        if errors:
            self.message_user(request, " ".join(errors), level=admin.ERROR)

