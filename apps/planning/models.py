"""planning/models.py"""

from django.db import models
from decimal import Decimal
from datetime import date, timedelta
from django.contrib.postgres.fields import ArrayField
from reference.models import CropBySeason, CropInfo, Variety


class PlanningYear(models.Model):
    year = models.PositiveIntegerField(unique=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ("planning", "Planning"),
            ("active", "Active"),
            ("complete", "Complete"),
            ("archived", "Archived"),
        ],
        default="planning",
    )
    overplant_factor = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.10"))

    def __str__(self):
        return f"{self.year} ({self.get_status_display()})"


class PlantingStatus(models.TextChoices):
    PLANNED = "planned", "Planned"
    SEEDED = "seeded", "Seeded (nursery)"
    PLANTED = "planted", "Planted"
    GROWING = "growing", "Growing"
    HARVESTING = "harvesting", "Harvesting"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"
    REVISED = "revised", "Revised"


class Planting(models.Model):
    planning_year = models.ForeignKey(
        PlanningYear, on_delete=models.CASCADE, related_name="plantings"
    )
    revision_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="revisions"
    )
    succession_group = models.CharField(max_length=50, blank=True)

    #: Human-facing durable code, e.g. ``P-2026-0001`` (unique; calendar year in prefix).
    planting_code = models.CharField(max_length=20, unique=True)

    crop = models.ForeignKey(CropInfo, on_delete=models.PROTECT)
    crop_season = models.ForeignKey(CropBySeason, on_delete=models.PROTECT)
    variety = models.CharField(max_length=100, blank=True)
    variety_obj = models.ForeignKey(
        Variety,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plantings",
    )
    block = models.ForeignKey("reference.Block", on_delete=models.PROTECT)
    bed_start = models.PositiveIntegerField()
    bed_end = models.PositiveIntegerField()

    # Planned
    planned_bedfeet = models.PositiveIntegerField()
    planned_plant_date = models.DateField()
    planned_first_harvest_date = models.DateField()
    planned_last_harvest_date = models.DateField()
    planned_total_yield = models.DecimalField(max_digits=10, decimal_places=2)

    # Actual
    actual_bedfeet = models.PositiveIntegerField(null=True, blank=True)
    actual_plant_date = models.DateField(null=True, blank=True)
    actual_first_harvest_date = models.DateField(null=True, blank=True)
    actual_last_harvest_date = models.DateField(null=True, blank=True)
    actual_total_yield = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    status = models.CharField(
        max_length=20, choices=PlantingStatus.choices, default=PlantingStatus.PLANNED
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def allocate_next_planting_code(
        cls, planning_year_id: int, calendar_year: int, *, exclude_pk: int | None = None
    ) -> str:
        """Next sequential code for a planning year: ``P-{year}-{NNNN}``."""
        prefix = f"P-{int(calendar_year)}-"
        qs = cls.objects.filter(planning_year_id=planning_year_id, planting_code__startswith=prefix)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        max_n = 0
        for code in qs.values_list("planting_code", flat=True):
            if not code or not str(code).startswith(prefix):
                continue
            tail = str(code)[len(prefix) :]
            try:
                max_n = max(max_n, int(tail))
            except ValueError:
                continue
        return f"{prefix}{max_n + 1:04d}"

    def _ensure_planting_code(self) -> None:
        if self.planting_code:
            return
        if not self.planning_year_id:
            return
        cal_year = None
        py = getattr(self, "planning_year", None)
        if py is not None and getattr(py, "year", None) is not None:
            cal_year = int(py.year)
        else:
            cal_year = PlanningYear.objects.filter(pk=self.planning_year_id).values_list("year", flat=True).first()
        if cal_year is None:
            return
        self.planting_code = self.allocate_next_planting_code(
            int(self.planning_year_id), int(cal_year), exclude_pk=self.pk if self.pk else None
        )

    def save(self, *args, **kwargs):
        old_actual_plant_date = None
        if self.pk:
            old_actual_plant_date = (
                Planting.objects.filter(pk=self.pk).values_list("actual_plant_date", flat=True).first()
            )
        self._ensure_planting_code()
        # Auto-calculate planned fields from crop_season
        if not self.planned_first_harvest_date and self.planned_plant_date:
            self.planned_first_harvest_date = self.planned_plant_date + timedelta(
                days=self.crop_season.dtm_days
            )
        if not self.planned_last_harvest_date and self.planned_first_harvest_date:
            self.planned_last_harvest_date = self.planned_first_harvest_date + timedelta(
                weeks=self.crop_season.harvest_weeks - 1
            )
        if not self.planned_total_yield:
            self.planned_total_yield = (
                self.planned_bedfeet * self.crop_season.total_yield_per_bedfoot
            )
        super().save(*args, **kwargs)
        self._apply_actual_plant_date_harvest_shift(old_actual_plant_date)

    PLANT_DATE_DRIFT_DAYS = 3

    def _apply_actual_plant_date_harvest_shift(self, old_actual_plant_date) -> None:
        """When actual_plant_date is first set and far from planned, shift pending harvest weeks."""
        new_date = self.actual_plant_date
        if new_date is None:
            return
        if old_actual_plant_date is not None:
            return
        delta = new_date - self.planned_plant_date
        if abs(delta.days) <= self.PLANT_DATE_DRIFT_DAYS:
            return
        shift = timedelta(days=delta.days)
        pending = self.harvest_events.filter(actual_quantity__isnull=True)
        for he in pending:
            he.planned_date = he.planned_date + shift
            he.save(update_fields=["planned_date"])

    def generate_nursery_events(self):
        """Create or refresh nursery events from crop info (idempotent)."""
        if self.crop.nursery_weeks == 0:
            return

        seed_date = self.planned_plant_date - timedelta(weeks=self.crop.nursery_weeks)
        NurseryEvent.objects.update_or_create(
            planting=self,
            event_type="seed",
            planned_date=seed_date,
            defaults={},
        )

        if self.crop.weeks_until_pot_up:
            pot_up_date = seed_date + timedelta(weeks=self.crop.weeks_until_pot_up)
            NurseryEvent.objects.update_or_create(
                planting=self,
                event_type="pot_up",
                planned_date=pot_up_date,
                defaults={},
            )

        NurseryEvent.objects.update_or_create(
            planting=self,
            event_type="transplant",
            planned_date=self.planned_plant_date,
            defaults={},
        )

    def generate_harvest_events(self):
        """Create planned weekly harvest events."""
        weekly_yield = self.crop_season.weekly_yield_per_bedfoot * self.planned_bedfeet
        current = self.planned_first_harvest_date
        while current <= self.planned_last_harvest_date:
            HarvestEvent.objects.create(
                planting=self,
                planned_date=current,
                planned_quantity=weekly_yield,
                planned_units=self.crop.harvest_unit,
            )
            current += timedelta(weeks=1)

    class Meta:
        ordering = ["planned_plant_date", "block__name"]
        indexes = [
            models.Index(
                fields=[
                    "planning_year",
                    "crop",
                    "block",
                    "bed_start",
                    "bed_end",
                    "planned_plant_date",
                ],
                name="planting_import_lookup_idx",
            ),
        ]


class SeedOrder(models.Model):
    """Stub seed order line per variety and planning year (sheet 402 Seed Order tab)."""

    variety = models.ForeignKey(Variety, on_delete=models.CASCADE, related_name="seed_order_lines")
    planning_year = models.ForeignKey(
        PlanningYear, on_delete=models.CASCADE, related_name="seed_orders"
    )
    planned_quantity = models.DecimalField(max_digits=12, decimal_places=2)
    unit = models.CharField(max_length=20, default="ounces")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["planning_year__year", "variety__crop__name", "variety__name"]

    def __str__(self):
        return f"{self.variety} ({self.planning_year.year}): {self.planned_quantity} {self.unit}"


class NurseryEvent(models.Model):
    EVENT_TYPES = [
        ("seed", "Seed"),
        ("pot_up", "Pot Up"),
        ("harden", "Harden Off"),
        ("transplant", "Transplant"),
    ]

    planting = models.ForeignKey(Planting, on_delete=models.CASCADE, related_name="nursery_events")
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)

    planned_date = models.DateField()
    planned_tray_count = models.PositiveIntegerField(null=True, blank=True)
    planned_tray_size = models.PositiveIntegerField(null=True, blank=True)

    actual_date = models.DateField(null=True, blank=True)
    actual_tray_count = models.PositiveIntegerField(null=True, blank=True)
    actual_tray_size = models.PositiveIntegerField(null=True, blank=True)
    actual_germination_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["planned_date", "planting"]

    @property
    def planned_week(self):
        return self.planned_date.isocalendar()[1]

    @property
    def is_complete(self):
        return self.actual_date is not None


class HarvestEvent(models.Model):
    planting = models.ForeignKey(Planting, on_delete=models.CASCADE, related_name="harvest_events")
    planned_date = models.DateField()

    planned_quantity = models.DecimalField(max_digits=10, decimal_places=2)
    planned_units = models.CharField(max_length=20)

    actual_date = models.DateField(null=True, blank=True)
    actual_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    actual_units = models.CharField(max_length=20, blank=True)
    actual_bins = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    actual_bin_type = models.CharField(max_length=50, blank=True)
    actual_hours = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    actual_workers = models.PositiveIntegerField(null=True, blank=True)

    quality_grade = models.CharField(
        max_length=20,
        blank=True,
        choices=[("prime", "Prime"), ("seconds", "Seconds"), ("mixed", "Mixed")],
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["planned_date", "planting"]

    @property
    def planned_week(self):
        return self.planned_date.isocalendar()[1]

    def save(self, *args, **kwargs):
        skip_inv = kwargs.pop("skip_inventory_ledger_sync", False)
        old_actual = None
        if self.pk:
            old_actual = (
                HarvestEvent.objects.filter(pk=self.pk)
                .values_list("actual_quantity", flat=True)
                .first()
            )
        super().save(*args, **kwargs)
        if skip_inv:
            return
        if self.actual_quantity is not None:
            from operations.services.inventory_ledger_sync import sync_harvest_event_ledger

            sync_harvest_event_ledger(self, old_actual)

    def record_bins(self, bin_count, bin_type=None):
        """Convert bin count to quantity using crop info."""
        self.actual_bins = bin_count
        if bin_type:
            self.actual_bin_type = bin_type
        else:
            self.actual_bin_type = self.planting.crop.harvest_bin

        units_per_bin = self.planting.crop.units_per_bin
        if units_per_bin:
            self.actual_quantity = bin_count * units_per_bin
        self.actual_units = self.planting.crop.harvest_unit
        self.actual_date = date.today()
        self.save()
