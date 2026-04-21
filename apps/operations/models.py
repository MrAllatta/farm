"""operations/models.py."""

from django.core.exceptions import ValidationError
from django.db import models
from decimal import Decimal
from reference.models import CropInfo
from reference.models import SalesChannel
from reference.models import CropSalesFormat
from reference.models import ProductRecipe
from planning.models import Planting
from planning.models import HarvestEvent


class FieldWalkNote(models.Model):
    CONDITION_CHOICES = [
        ("good", "Good"),
        ("fair", "Fair"),
        ("poor", "Poor"),
        ("failed", "Failed"),
    ]

    planting = models.ForeignKey(
        Planting, on_delete=models.CASCADE, related_name="field_walk_notes"
    )
    walk_date = models.DateField()

    condition = models.CharField(max_length=10, choices=CONDITION_CHOICES)
    adjusted_first_harvest_date = models.DateField(null=True, blank=True)
    adjusted_last_harvest_date = models.DateField(null=True, blank=True)
    yield_adjust_pct = models.PositiveIntegerField(
        default=100, help_text="100 = no change, 50 = half expected yield"
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-walk_date", "planting__block__walk_route_order"]

    @property
    def walk_week(self):
        return self.walk_date.isocalendar()[1]


class InventoryLedger(models.Model):
    EVENT_TYPES = [
        ("harvest_in", "Harvest In"),
        ("sale_out", "Sale Out"),
        ("return_in", "Return In"),
        ("waste_out", "Waste Out"),
        ("transfer", "Transfer"),
        ("quality_check", "Quality Check"),
        ("year_end_count", "Year End Count"),
        ("adjustment", "Adjustment"),
    ]

    crop = models.ForeignKey(CropInfo, on_delete=models.PROTECT)
    harvest_event = models.ForeignKey(
        "planning.HarvestEvent", on_delete=models.SET_NULL, null=True, blank=True
    )

    event_date = models.DateField()
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    # positive for in, negative for out

    running_balance = models.DecimalField(max_digits=10, decimal_places=2)
    expiry_date = models.DateField(null=True, blank=True)
    storage_location = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["crop__name", "event_date", "created_at", "id"]

    def save(self, *args, **kwargs):
        if not self.running_balance:
            # Calculate from previous entry
            last = (
                InventoryLedger.objects.filter(crop=self.crop, event_date__lte=self.event_date)
                .exclude(pk=self.pk)
                .order_by("-event_date", "-created_at", "-id")
                .first()
            )

            prev_balance = last.running_balance if last else Decimal("0")
            self.running_balance = prev_balance + self.quantity
        super().save(*args, **kwargs)


class PackAllocation(models.Model):
    pack_batch = models.ForeignKey(
        "PackBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="allocations",
    )
    harvest_event = models.ForeignKey(
        HarvestEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pack_allocations",
    )
    inventory_draw = models.ForeignKey(
        InventoryLedger, on_delete=models.SET_NULL, null=True, blank=True
    )
    channel = models.ForeignKey(SalesChannel, on_delete=models.PROTECT)
    product = models.ForeignKey(CropSalesFormat, on_delete=models.PROTECT)

    pack_date = models.DateField()
    quantity = models.DecimalField(max_digits=10, decimal_places=2)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["pack_date", "channel__allocation_priority"]


class PackBatch(models.Model):
    product = models.ForeignKey(CropSalesFormat, on_delete=models.PROTECT, related_name="pack_batches")
    recipe = models.ForeignKey(
        ProductRecipe,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pack_batches",
    )
    packed_quantity = models.DecimalField(max_digits=10, decimal_places=2)
    packed_unit = models.CharField(max_length=20)
    pack_date = models.DateField()
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pack_date", "-id"]

    def __str__(self):
        return f"{self.product.product_name} batch {self.pack_date} ({self.packed_quantity} {self.packed_unit})"

    def clean(self):
        if self.packed_quantity <= 0:
            raise ValidationError({"packed_quantity": "Packed quantity must be positive."})
        if self.recipe and self.recipe.product_id != self.product_id:
            raise ValidationError({"recipe": "Recipe must belong to the selected product."})

    def post_component_consumption(self):
        """
        Write deterministic inventory drawdown for crop-backed component rows.

        TODO: event_type uses ``sale_out`` today; a dedicated ``pack_out`` (or
        nested finished-goods ledger) would separate mix consumption from customer sales.
        """
        components = self.components.select_related("source_crop").all()
        if not components.exists():
            raise ValidationError("Pack batch must include at least one component consumption row.")

        created_entries = []
        for component in components:
            if component.source_crop_id is None:
                # Product-backed component consumption may be modeled later as nested ledger flow.
                continue

            qty = -abs(component.consumed_quantity)
            last = (
                InventoryLedger.objects.filter(crop=component.source_crop)
                .order_by("-event_date", "-created_at", "-id")
                .first()
            )
            previous_balance = last.running_balance if last else Decimal("0")
            running_balance = previous_balance + qty

            entry = InventoryLedger.objects.create(
                crop=component.source_crop,
                event_date=self.pack_date,
                event_type="sale_out",
                quantity=qty,
                running_balance=running_balance,
                notes=f"Mix consumption for pack batch {self.id}",
            )
            component.inventory_ledger_entry = entry
            component.save(update_fields=["inventory_ledger_entry"])
            created_entries.append(entry)
        return created_entries


class PackBatchComponent(models.Model):
    pack_batch = models.ForeignKey(
        PackBatch,
        on_delete=models.CASCADE,
        related_name="components",
    )
    source_crop = models.ForeignKey(
        CropInfo,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pack_batch_components",
    )
    source_product = models.ForeignKey(
        CropSalesFormat,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pack_batch_product_components",
    )
    consumed_quantity = models.DecimalField(max_digits=10, decimal_places=2)
    consumed_unit = models.CharField(max_length=20)
    component_percent = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    inventory_ledger_entry = models.ForeignKey(
        InventoryLedger,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mix_component_rows",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["pack_batch", "id"]

    def clean(self):
        if bool(self.source_crop) == bool(self.source_product):
            raise ValidationError("Provide exactly one component source: crop or product.")
        if self.consumed_quantity <= 0:
            raise ValidationError({"consumed_quantity": "Consumed quantity must be positive."})
