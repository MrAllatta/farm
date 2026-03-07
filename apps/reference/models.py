"""reference/models.py data models for farm references."""

from django.db import models
import math
from decimal import Decimal
from django.contrib.postgres.fields import ArrayField


class CropInfo(models.Model):
    name = models.CharField(max_length=100, unique=True)
    crop_type = models.CharField(max_length=50)  # "Tomatoes", "Roots", etc.
    botanical_family = models.CharField(max_length=50, blank=True)
    propagation_type = models.CharField(
        max_length=20,
        choices=[
            ("seed", "Seed"),
            ("vegetative_clove", "Clove"),
            ("vegetative_tuber", "Tuber"),
            ("vegetative_slip", "Slip"),
        ],
        default="seed",
    )
    is_perennial = models.BooleanField(default=False)
    fresh_or_storage = models.CharField(
        max_length=10, choices=[("fresh", "Fresh"), ("storage", "Storage")]
    )
    storage_weeks = models.PositiveIntegerField(default=0)
    can_hold_in_field = models.BooleanField(default=False)
    harvest_unit = models.CharField(max_length=20)  # "pounds", "bunches", "each"
    avg_unit_weight = models.DecimalField(max_digits=5, decimal_places=2)
    units_per_bin = models.PositiveIntegerField(null=True, blank=True)
    harvest_bin = models.CharField(max_length=50, blank=True)
    harvest_tools = models.CharField(max_length=100, blank=True)
    harvest_rate_per_hour = models.PositiveIntegerField(null=True, blank=True)

    # Nursery
    nursery_weeks = models.PositiveIntegerField(default=0)
    weeks_until_pot_up = models.PositiveIntegerField(default=0)
    pot_up_tray_size = models.PositiveIntegerField(null=True, blank=True)
    seeded_tray_size = models.PositiveIntegerField(null=True, blank=True)
    seeds_per_cell = models.PositiveIntegerField(default=1)
    thinned_plants = models.PositiveIntegerField(default=0)
    seeds_per_ounce = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BlockType(models.TextChoices):
    FIELD = "field", "Field"
    HIGH_TUNNEL = "high_tunnel", "High Tunnel"
    GREENHOUSE = "greenhouse", "Greenhouse"


class Block(models.Model):
    name = models.CharField(max_length=20, unique=True)
    block_type = models.CharField(max_length=20, choices=BlockType.choices)
    num_beds = models.PositiveIntegerField()
    bed_width_feet = models.DecimalField(max_digits=4, decimal_places=1)
    bedfeet_per_bed = models.PositiveIntegerField()
    walk_route_order = models.PositiveIntegerField(default=0)

    @property
    def total_bedfeet(self):
        return self.num_beds * self.bedfeet_per_bed

    @property
    def square_feet(self):
        return self.total_bedfeet * self.bed_width_feet

    class Meta:
        ordering = ["walk_route_order", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_block_type_display()})"


class CropBySeason(models.Model):
    crop = models.ForeignKey(CropInfo, on_delete=models.CASCADE, related_name="season_profiles")
    block_type = models.CharField(max_length=20, choices=BlockType.choices)

    field_week_start = models.PositiveIntegerField()
    field_week_end = models.PositiveIntegerField()

    total_yield_per_bedfoot = models.DecimalField(max_digits=6, decimal_places=2)
    harvest_weeks = models.PositiveIntegerField()
    dtm_days = models.PositiveIntegerField()

    rows_per_bed = models.PositiveIntegerField()
    ds_seed_rate = models.PositiveIntegerField(null=True, blank=True)
    tp_inrow_spacing = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    seeder_settings = models.CharField(max_length=200, blank=True)
    trellis_system = models.CharField(max_length=100, blank=True)
    mulch = models.CharField(max_length=50, blank=True)
    row_cover = models.CharField(max_length=50, blank=True)
    irrigation = models.CharField(max_length=50, blank=True)

    @property
    def wtm_weeks(self):
        return math.ceil(self.dtm_days / 7)

    @property
    def weekly_yield_per_bedfoot(self):
        if self.harvest_weeks:
            return self.total_yield_per_bedfoot / self.harvest_weeks
        return Decimal("0")

    class Meta:
        unique_together = ["crop", "block_type"]
        ordering = ["crop__name", "block_type"]

    def __str__(self):
        return f"{self.crop.name} / {self.get_block_type_display()}"


class CropSalesFormat(models.Model):
    crop = models.ForeignKey(CropInfo, on_delete=models.CASCADE, related_name="sales_formats")
    product_name = models.CharField(max_length=100)
    sale_price = models.DecimalField(max_digits=8, decimal_places=2)
    sale_unit = models.CharField(max_length=20)  # "each", "pound", "bunch", "pint", "bag"
    harvest_qty_per_sale_unit = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("1.00")
    )
    sku = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["crop__name", "product_name"]

    def __str__(self):
        return f"{self.product_name} @ ${self.sale_price}/{self.sale_unit}"


class SalesChannel(models.Model):
    name = models.CharField(max_length=100)
    days_of_week = ArrayField(
        models.CharField(max_length=10), default=list
    )  # PostgreSQL array field
    start_week = models.PositiveIntegerField()
    end_week = models.PositiveIntegerField()
    weekly_target = models.DecimalField(max_digits=10, decimal_places=2)
    is_csa = models.BooleanField(default=False)
    allocation_priority = models.PositiveIntegerField(default=10)

    @property
    def num_weeks(self):
        if self.end_week >= self.start_week:
            return self.end_week - self.start_week + 1
        return (52 - self.start_week + 1) + self.end_week

    @property
    def annual_target(self):
        return self.weekly_target * self.num_weeks

    class Meta:
        ordering = ["allocation_priority", "name"]

    def __str__(self):
        return self.name
