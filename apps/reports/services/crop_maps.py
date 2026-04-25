"""Shared occupancy builders for 503-style crop map views."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from isoweek import Week

from operations.planting_display import (
    planting_map_segment_label,
    planting_unit_code,
    planting_unit_full_label,
    planting_variety_display,
)

from planning.models import Planting
from reference.models import Block


EXCLUDED_STATUSES = ("skipped", "revised", "failed")


@dataclass(frozen=True)
class WeekLabel:
    """Lightweight week metadata for grid headers."""

    num: int
    monday: date
    month: str
    is_current: bool


def _status_css_for_week(planting, week_start):
    """Return a display status class for a planting in a given week."""
    if planting.status == "complete":
        return "planting-complete"
    if week_start >= planting.planned_first_harvest_date:
        return "planting-harvesting"
    if week_start >= planting.planned_plant_date:
        return "planting-growing"
    return "planting-planned"


class CropMapOccupancyService:
    """Build status-aware occupancy structures reused by crop map views."""

    def __init__(self, planning_year):
        self.planning_year = planning_year
        self.blocks = list(Block.objects.all().order_by("walk_route_order", "name"))
        self.plantings = list(
            Planting.objects.filter(planning_year=planning_year)
            .exclude(status__in=EXCLUDED_STATUSES)
            .select_related("crop", "block", "crop_season", "variety_obj")
            .order_by("block__walk_route_order", "block__name", "bed_start", "planned_plant_date")
        )

    @staticmethod
    def build_week_labels(year, week_start=1, week_end=52):
        today_week = date.today().isocalendar()[1]
        labels = []
        for wk in range(week_start, week_end + 1):
            monday = Week(year, wk).monday()
            labels.append(
                WeekLabel(
                    num=wk,
                    monday=monday,
                    month=monday.strftime("%b"),
                    is_current=(wk == today_week),
                )
            )
        return labels

    def get_high_level_block_map(self, week_num):
        week_start = Week(self.planning_year.year, week_num).monday()
        week_end = week_start + timedelta(days=6)

        rows = []
        for block in self.blocks:
            active = [
                p
                for p in self.plantings
                if p.block_id == block.id
                and p.planned_plant_date <= week_end
                and p.planned_last_harvest_date >= week_start
            ]
            covered_beds = set()
            segments = []
            for planting in active:
                for bed in range(planting.bed_start, planting.bed_end + 1):
                    covered_beds.add(bed)
                crop_key = planting.crop.crop_type.lower().replace("/", "-").replace(" ", "-")
                vdisp = planting_variety_display(planting)
                crop_label = planting.crop.name if not vdisp else f"{planting.crop.name} — {vdisp}"
                segments.append(
                    {
                        "planting": planting,
                        "bed_start": planting.bed_start,
                        "bed_end": planting.bed_end,
                        "label": planting_map_segment_label(planting),
                        "crop_label": crop_label,
                        "tooltip": planting_unit_full_label(planting),
                        "status_css": _status_css_for_week(planting, week_start),
                        "crop_css": f"crop-{crop_key}",
                        "width_pct": ((planting.bed_end - planting.bed_start + 1) / block.num_beds * 100)
                        if block.num_beds
                        else 0,
                    }
                )

            fallow = self._build_fallow_segments(block, covered_beds)
            all_segments = sorted(segments + fallow, key=lambda row: row["bed_start"])
            rows.append(
                {
                    "block": block,
                    "segments": all_segments,
                    "active_plantings": len(active),
                    "utilization_pct": (len(covered_beds) / block.num_beds * 100) if block.num_beds else 0,
                }
            )
        return rows

    def get_week_by_bed_grid(self, week_start=1, week_end=52):
        week_labels = self.build_week_labels(self.planning_year.year, week_start=week_start, week_end=week_end)
        plantings_by_block = defaultdict(list)
        for planting in self.plantings:
            plantings_by_block[planting.block_id].append(planting)

        rows = []
        for block in self.blocks:
            block_rows = []
            for bed in range(1, block.num_beds + 1):
                cells = []
                for wk_idx, wk in enumerate(week_labels):
                    week_end_date = wk.monday + timedelta(days=6)
                    candidates = [
                        p
                        for p in plantings_by_block.get(block.id, [])
                        if p.bed_start <= bed <= p.bed_end
                        and p.planned_plant_date <= week_end_date
                        and p.planned_last_harvest_date >= wk.monday
                    ]
                    candidates.sort(key=lambda p: (-(p.bed_end - p.bed_start + 1), -p.pk))
                    occupant = candidates[0] if candidates else None
                    prev_week_occ = cells[wk_idx - 1]["planting"] if wk_idx > 0 else None
                    prev_bed_occ = None
                    if bed > 1 and block_rows:
                        prev_cells = block_rows[bed - 2]["cells"]
                        if wk_idx < len(prev_cells):
                            prev_bed_occ = prev_cells[wk_idx]["planting"]
                    h_cont = occupant is not None and prev_week_occ == occupant
                    v_cont = occupant is not None and prev_bed_occ == occupant
                    continuation = occupant is not None and (h_cont or v_cont)
                    show_primary = occupant is not None and not continuation
                    cells.append(
                        {
                            "week": wk.num,
                            "week_monday": wk.monday,
                            "planting": occupant,
                            "is_fallow": occupant is None,
                            "status_css": _status_css_for_week(occupant, wk.monday) if occupant else "fallow",
                            "continuation": continuation,
                            "show_primary_label": show_primary,
                            "primary_label": planting_unit_code(occupant) if occupant else "",
                            "tooltip": planting_unit_full_label(occupant) if occupant else "",
                        }
                    )
                block_rows.append({"bed_num": bed, "cells": cells})
            rows.append({"block": block, "bed_rows": block_rows})
        return {"weeks": week_labels, "rows": rows}

    def get_week_by_block_grid(self, week_start=1, week_end=52):
        week_labels = self.build_week_labels(self.planning_year.year, week_start=week_start, week_end=week_end)
        rows = []
        for block in self.blocks:
            cells = []
            for wk in week_labels:
                week_end_date = wk.monday + timedelta(days=6)
                active = [
                    p
                    for p in self.plantings
                    if p.block_id == block.id
                    and p.planned_plant_date <= week_end_date
                    and p.planned_last_harvest_date >= wk.monday
                ]
                used_beds = set()
                for planting in active:
                    for bed in range(planting.bed_start, planting.bed_end + 1):
                        used_beds.add(bed)
                active_sorted = sorted(active, key=lambda p: (p.bed_start, p.pk))
                max_chips = 10
                chips = [
                    {
                        "planting": p,
                        "code": planting_unit_code(p),
                        "crop": p.crop.name,
                    }
                    for p in active_sorted[:max_chips]
                ]
                cells.append(
                    {
                        "week": wk.num,
                        "week_monday": wk.monday,
                        "plantings": active,
                        "chips": chips,
                        "more_plantings": max(0, len(active_sorted) - max_chips),
                        "active_count": len(active),
                        "used_beds": len(used_beds),
                        "utilization_pct": (len(used_beds) / block.num_beds * 100) if block.num_beds else 0,
                        "is_empty": not active,
                    }
                )
            rows.append({"block": block, "cells": cells})
        return {"weeks": week_labels, "rows": rows}

    def get_successions_by_block(self):
        rows = []
        for block in self.blocks:
            block_plantings = [p for p in self.plantings if p.block_id == block.id]
            grouped = defaultdict(list)
            for planting in block_plantings:
                vlabel = planting_variety_display(planting) or "standard"
                key = planting.succession_group or f"{planting.crop.name}-{vlabel}"
                grouped[key].append(planting)

            succession_rows = []
            for succession_key, items in sorted(grouped.items(), key=lambda item: item[0].lower()):
                sorted_items = sorted(items, key=lambda p: p.planned_plant_date)
                succession_rows.append(
                    {
                        "succession_key": succession_key,
                        "crop_name": sorted_items[0].crop.name,
                        "variety": planting_variety_display(sorted_items[0]),
                        "plantings": sorted_items,
                        "count": len(sorted_items),
                        "first_week": sorted_items[0].planned_plant_date.isocalendar()[1],
                        "last_week": sorted_items[-1].planned_plant_date.isocalendar()[1],
                        "total_bedfeet": sum(p.planned_bedfeet for p in sorted_items),
                    }
                )
            rows.append({"block": block, "successions": succession_rows})
        return rows

    @staticmethod
    def _build_fallow_segments(block, covered_beds):
        all_beds = set(range(1, block.num_beds + 1))
        missing = sorted(all_beds - covered_beds)
        if not missing:
            return []

        segments = []
        start = prev = missing[0]
        for bed in missing[1:]:
            if bed == prev + 1:
                prev = bed
                continue
            segments.append(
                {
                    "bed_start": start,
                    "bed_end": prev,
                    "label": "fallow",
                    "status_css": "fallow",
                    "crop_css": "",
                    "width_pct": ((prev - start + 1) / block.num_beds * 100) if block.num_beds else 0,
                }
            )
            start = prev = bed

        segments.append(
            {
                "bed_start": start,
                "bed_end": prev,
                "label": "fallow",
                "status_css": "fallow",
                "crop_css": "",
                "width_pct": ((prev - start + 1) / block.num_beds * 100) if block.num_beds else 0,
            }
        )
        return segments
