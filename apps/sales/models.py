"""sales/models.py"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from reference.models import SalesChannel
from reference.models import CropSalesFormat
from planning.models import PlanningYear


class SalesEvent(models.Model):
    class EntryKind(models.TextChoices):
        PLAN = "plan", "Plan"
        ACTUAL = "actual", "Actual"

    # Operational outlet (required for actuals; required for outlet-level weekly plans).
    channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    # Workbook 302 / category-level annual demand (Markets, Orders, CSA) — mutually exclusive
    # with ``channel`` for *category-only* plan rows (channel is null).
    sales_category = models.ForeignKey(
        "reference.SalesCategory",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales_events",
    )
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
    pack_batch = models.ForeignKey(
        "operations.PackBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_events",
    )
    drawn_from_return = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resale_draws",
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

    def clean(self):
        super().clean()
        if self.entry_kind == self.EntryKind.ACTUAL:
            if self.channel_id is None:
                raise ValidationError({"channel": "Actual sales events require a sales channel."})
        elif self.entry_kind == self.EntryKind.PLAN:
            if self.channel_id is None and self.sales_category_id is None:
                raise ValidationError(
                    "Planned sales events require either a sales channel or a sales category."
                )

    def save(self, *args, **kwargs):
        skip_inv = kwargs.pop("skip_inventory_ledger_sync", False)
        old_actual = None
        old_returned = None
        if self.pk:
            prev = (
                SalesEvent.objects.filter(pk=self.pk)
                .values("actual_quantity", "returned_quantity")
                .first()
            )
            if prev:
                old_actual = prev["actual_quantity"]
                old_returned = prev["returned_quantity"]
        super().save(*args, **kwargs)
        if skip_inv:
            return
        from operations.services.inventory_ledger_sync import sync_sales_event_ledger

        sync_sales_event_ledger(self, old_actual, old_returned)

    class Meta:
        ordering = ["sale_date", "channel_id", "sales_category_id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(entry_kind="plan", channel__isnull=True, sales_category__isnull=True)
                    & ~Q(entry_kind="actual", channel__isnull=True)
                ),
                name="salesevent_plan_or_actual_needs_channel_or_cat",
            ),
            models.UniqueConstraint(
                fields=["entry_kind", "channel", "sale_date", "product"],
                condition=Q(entry_kind="plan", channel__isnull=False),
                name="salesevent_uniq_plan_channel_date_product",
            ),
            models.UniqueConstraint(
                fields=["entry_kind", "sales_category", "sale_date", "product"],
                condition=Q(entry_kind="plan", channel__isnull=True, sales_category__isnull=False),
                name="salesevent_uniq_plan_category_date_product",
            ),
            models.UniqueConstraint(
                fields=["entry_kind", "channel", "sale_date", "product"],
                condition=Q(entry_kind="actual"),
                name="salesevent_uniq_actual_channel_date_product",
            ),
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
