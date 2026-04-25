"""Idempotent repair of generated nursery and harvest events for plantings.

Backlog: OP-7, LC-5 — imported or legacy plantings may exist without
``HarvestEvent`` / ``NurseryEvent`` rows; week-ops and sales shortage views
depend on those rows. This module only **creates** missing generated rows;
it does not delete or overwrite rows that already exist (including CSV-imported
events with ``actual_quantity`` set).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Exists, F, OuterRef

from planning.models import HarvestEvent, NurseryEvent, Planting, PlantingStatus


EXCLUDED_PLANTING_STATUSES = (
    PlantingStatus.SKIPPED,
    PlantingStatus.FAILED,
    PlantingStatus.REVISED,
)


@dataclass
class PlantingEventsRepairStats:
    plantings_scanned: int = 0
    harvest_events_created_plantings: int = 0
    nursery_events_created_plantings: int = 0
    harvest_skipped_invalid_window: int = 0
    harvest_planned_dates_filled: int = 0

    def as_dict(self) -> dict:
        return {
            "plantings_scanned": self.plantings_scanned,
            "harvest_events_created_plantings": self.harvest_events_created_plantings,
            "nursery_events_created_plantings": self.nursery_events_created_plantings,
            "harvest_skipped_invalid_window": self.harvest_skipped_invalid_window,
            "harvest_planned_dates_filled": self.harvest_planned_dates_filled,
        }


def repair_planting_events(
    *,
    planning_year_ids: list[int] | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
) -> PlantingEventsRepairStats:
    """Ensure each eligible planting has generated harvest and nursery events when missing.

    - Harvest: create weekly events only when the planting has **zero** harvest events and a
      valid planned harvest window.
    - Nursery: call ``generate_nursery_events`` only when ``crop.nursery_weeks > 0`` and the
      planting has **zero** nursery events (avoids duplicating partial imports).
    """
    stats = PlantingEventsRepairStats()

    qs = (
        Planting.objects.select_related("crop", "crop_season", "planning_year")
        .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
        .order_by("id")
    )
    if planning_year_ids:
        qs = qs.filter(planning_year_id__in=planning_year_ids)
    if min_year is not None:
        qs = qs.filter(planning_year__year__gte=min_year)
    if max_year is not None:
        qs = qs.filter(planning_year__year__lte=max_year)

    no_harvest = ~Exists(HarvestEvent.objects.filter(planting_id=OuterRef("pk")))
    no_nursery = ~Exists(NurseryEvent.objects.filter(planting_id=OuterRef("pk")))

    for planting in qs.annotate(_no_harvest=no_harvest, _no_nursery=no_nursery).iterator(
        chunk_size=500
    ):
        stats.plantings_scanned += 1

        if planting._no_harvest:
            # Imported plantings often miss harvest dates: ``update_or_create`` updates
            # only ``defaults`` keys, so ``Planting.save()`` auto-fill never ran. Fill from
            # ``crop_season`` here, persist, then generate weekly events.
            if planting.fill_missing_planned_harvest_dates() and planting.pk:
                Planting.objects.filter(pk=planting.pk).update(
                    planned_first_harvest_date=planting.planned_first_harvest_date,
                    planned_last_harvest_date=planting.planned_last_harvest_date,
                )
                stats.harvest_planned_dates_filled += 1

            first_d = planting.planned_first_harvest_date
            last_d = planting.planned_last_harvest_date
            if first_d and last_d and first_d <= last_d:
                planting.generate_harvest_events()
                stats.harvest_events_created_plantings += 1
            else:
                stats.harvest_skipped_invalid_window += 1

        if (planting.crop.nursery_weeks or 0) > 0 and planting._no_nursery:
            planting.generate_nursery_events()
            stats.nursery_events_created_plantings += 1

    return stats


def count_plantings_missing_harvest_events(planning_year_id: int) -> int:
    """How many non-excluded plantings have zero harvest events (diagnostics for OP-8)."""
    no_harvest = ~Exists(HarvestEvent.objects.filter(planting_id=OuterRef("pk")))
    return (
        Planting.objects.filter(planning_year_id=planning_year_id)
        .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
        .annotate(_no_harvest=no_harvest)
        .filter(_no_harvest=True)
        .filter(
            planned_first_harvest_date__isnull=False,
            planned_last_harvest_date__isnull=False,
        )
        .filter(planned_first_harvest_date__lte=F("planned_last_harvest_date"))
        .count()
    )
