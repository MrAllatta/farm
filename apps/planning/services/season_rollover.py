"""Skeleton rollover: copy planned plantings into a new planning year (+52 weeks, no actuals)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from django.db import transaction

from planning.models import PlanningYear, Planting, PlantingStatus

SHIFT = timedelta(weeks=52)


@dataclass(frozen=True)
class RolloverSummary:
    """Counts for preview (dry run) or post-commit reporting."""

    source_year: int
    target_year: int
    num_plantings: int
    num_blocks: int
    total_bedfeet: int
    dry_run: bool
    message: str = ""


def _planting_queryset(source: PlanningYear):
    return (
        Planting.objects.filter(planning_year=source)
        .exclude(status__in=["skipped", "failed", "revised"])
        .select_related("crop", "crop_season", "block", "variety_obj")
    )


def summarize_skeleton(source: PlanningYear, target_year: int, *, dry_run: bool) -> RolloverSummary:
    """Dry-run counts for preview UI."""
    plantings = list(_planting_queryset(source))
    block_ids = {p.block_id for p in plantings}
    total_bf = sum(p.planned_bedfeet for p in plantings)
    return RolloverSummary(
        source_year=source.year,
        target_year=target_year,
        num_plantings=len(plantings),
        num_blocks=len(block_ids),
        total_bedfeet=total_bf,
        dry_run=dry_run,
    )


def copy_skeleton(
    source: PlanningYear,
    target: PlanningYear,
    *,
    dry_run: bool = False,
) -> RolloverSummary:
    """
    Copy planned layout (crop, season profile, block, beds, bedfeet) into ``target``.

    - Shifts planned plant + harvest window dates by +52 weeks.
    - New plantings are ``planned`` with no actuals; nursery + harvest events are regenerated.
    - Refuses non-dry-run if ``target`` already has any plantings (idempotent guard).
    """
    summary = summarize_skeleton(source, target.year, dry_run=dry_run)
    if dry_run:
        return summary

    plantings = list(_planting_queryset(source))
    block_ids = {p.block_id for p in plantings}

    if Planting.objects.filter(planning_year=target).exists():
        raise ValueError(
            f"Planning year {target.year} already has plantings; clear it or pick another year."
        )

    with transaction.atomic():
        for p in plantings:
            np = Planting(
                planning_year=target,
                crop=p.crop,
                crop_season=p.crop_season,
                variety=p.variety,
                variety_obj_id=p.variety_obj_id,
                block=p.block,
                bed_start=p.bed_start,
                bed_end=p.bed_end,
                planned_bedfeet=p.planned_bedfeet,
                planned_plant_date=p.planned_plant_date + SHIFT,
                planned_first_harvest_date=p.planned_first_harvest_date + SHIFT,
                planned_last_harvest_date=p.planned_last_harvest_date + SHIFT,
                planned_total_yield=p.planned_total_yield,
                status=PlantingStatus.PLANNED,
                notes="",
                succession_group=p.succession_group or "",
            )
            np.save()
            np.generate_nursery_events()
            np.generate_harvest_events()

    return RolloverSummary(
        source_year=source.year,
        target_year=target.year,
        num_plantings=len(plantings),
        num_blocks=len(block_ids),
        total_bedfeet=summary.total_bedfeet,
        dry_run=False,
        message=f"Copied {len(plantings)} plantings into {target.year}.",
    )
