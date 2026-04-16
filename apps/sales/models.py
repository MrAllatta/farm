"""sales/models.py"""

from django.db import models
from reference.models import SalesChannel
from reference.models import CropSalesFormat
from planning.models import PlanningYear


class SalesEvent(models.Model):
    class EntryKind(models.TextChoices):
        PLAN = "plan", "Plan"
        ACTUAL = "actual", "Actual"

    channel = models.ForeignKey(SalesChannel, on_delete=models.PROTECT)
    sale_date = models.DateField()
    planning_year = models.ForeignKey(
        PlanningYear,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales_events",
    )
    entry_kind = models.CharField(
        max_length=10,
        choices=EntryKind.choices,
        default=EntryKind.ACTUAL,
    )
    product = models.ForeignKey(
        CropSalesFormat,
        on_delete=models.PROTECT,
        null=True,
        blank=True,  # null for quick-entry (total only)
    )

    planned_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    planned_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    actual_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    actual_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    actual_price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    brought_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    returned_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    notes = models.TextField(blank=True)

    @property
    def sell_through_pct(self):
        if self.brought_quantity and self.brought_quantity > 0:
            sold = self.actual_quantity or (self.brought_quantity - (self.returned_quantity or 0))
            return sold / self.brought_quantity * 100
        return None

    @property
    def sale_week(self):
        return self.sale_date.isocalendar()[1]

    class Meta:
        ordering = ["sale_date", "channel"]
        constraints = [
            models.UniqueConstraint(
                fields=["entry_kind", "channel", "sale_date", "product"],
                name="sales_event_kind_channel_date_product_uniq",
            )
        ]


class QuickSalesEntry(models.Model):
    """For farmers who just want to record total revenue per market day."""

    channel = models.ForeignKey("reference.SalesChannel", on_delete=models.PROTECT)
    sale_date = models.DateField()

    total_cash = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_card = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    notes = models.TextField(blank=True)

    @property
    def total_revenue(self):
        return self.total_cash + self.total_card

    class Meta:
        unique_together = ["channel", "sale_date"]
        ordering = ["sale_date", "channel"]
