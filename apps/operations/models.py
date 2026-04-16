"""operations/models.py."""

from django.db import models
from decimal import Decimal
from reference.models import CropInfo
from reference.models import SalesChannel
from reference.models import CropSalesFormat
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
