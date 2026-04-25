"""Parse workbook 402 ``Nursery Plan 502`` wide rows and resolve ``Planting`` rows.

Nursery *events* are derived in-app from ``CropInfo`` + ``Planting``; this module
supports read-only parity checks against exported ``nursery_events.csv`` rows.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from planning.models import Planting
from reference.models import Block, CropInfo


def normalize_csv_header_label(header) -> str:
    if header is None:
        return ""
    return " ".join(str(header).replace("\n", " ").strip().lower().split())


def nursery_row_is_workbook_402_plan_tab_shape(row: dict) -> bool:
    """True when row carries ``Nursery Plan 502`` week columns (wide shape)."""
    for k in row:
        nk = normalize_csv_header_label(k)
        if nk in {
            "nursery seeding year",
            "nursery seeding week",
            "nursery pot up year",
            "nursery pot up week",
        }:
            if (row.get(k) or "").strip():
                return True
    return False


def nursery_sheet_crop_variety_cell(row: dict) -> str:
    for key in row:
        nk = normalize_csv_header_label(key)
        if nk in ("crop // variety", "crop & variety"):
            raw = row.get(key) or ""
            return " ".join(str(raw).replace("\n", " ").split()).strip()
    return ""


def split_crop_variety(crop_variety: str) -> tuple[str, str]:
    """Split ``Crop // Variety`` text into crop and variety."""
    crop_variety = " ".join(str(crop_variety).replace("\n", " ").split())
    if "//" in crop_variety:
        left, right = crop_variety.split("//", 1)
        return left.strip(), right.strip()
    return crop_variety.strip(), ""


def split_crop_variety_for_nursery_sheet(cell: str) -> tuple[str, str]:
    cell = " ".join(cell.replace("\n", " ").split())
    if "//" in cell:
        return split_crop_variety(cell)
    if " & " in cell:
        left, right = cell.split(" & ", 1)
        return left.strip(), right.strip()
    return cell, ""


def date_from_plan_year_week(year_raw, week_raw) -> date | None:
    try:
        y = int(str(year_raw).strip())
        w = int(str(week_raw).strip())
        if y <= 0 or w <= 0:
            return None
        return date.fromisocalendar(y, w, 1)
    except (ValueError, TypeError, OverflowError):
        return None


def resolve_field_walk_planting_from_context(row: dict, year: int) -> Planting | None:
    """Match a unique planting from block/bed + crop label + plan field ISO week."""
    crop_variety = (row.get("Crop // Variety") or "").strip()
    block_name = (row.get("Block") or "").strip()
    bed_raw = (row.get("Bed") or "").strip()
    plan_year_raw = (row.get("Plan Field Year") or "").strip()
    plan_week_raw = (row.get("Plan Field Week") or "").strip()

    if not (crop_variety and block_name and bed_raw and plan_year_raw and plan_week_raw):
        return None

    crop_name, variety = split_crop_variety(crop_variety)
    if not crop_name:
        return None

    try:
        plan_year = int(str(plan_year_raw).strip())
        plan_week = int(str(plan_week_raw).strip())
    except ValueError:
        return None
    if plan_year <= 0 or plan_week <= 0 or plan_year != year:
        return None

    try:
        bed_number = int(str(bed_raw).strip())
    except ValueError:
        return None
    if bed_number <= 0:
        return None

    block = Block.objects.filter(name__iexact=block_name).first()
    crop = CropInfo.objects.filter(name__iexact=crop_name).first()
    if not block or not crop:
        return None

    candidates = (
        Planting.objects.filter(
            planning_year__year=plan_year,
            crop=crop,
            block=block,
            bed_start__lte=bed_number,
            bed_end__gte=bed_number,
        )
        .select_related("crop", "block", "planning_year")
        .order_by("id")
    )

    if variety:
        candidates = candidates.filter(variety__iexact=variety)

    matches = []
    for planting in candidates:
        iso_year, iso_week, _ = planting.planned_plant_date.isocalendar()
        if iso_year == plan_year and iso_week == plan_week:
            matches.append(planting)

    if len(matches) == 1:
        return matches[0]
    return None


def resolve_nursery_planting_from_plan_tab(row: dict, year: int) -> Planting | None:
    """Resolve a planting for ``Nursery Plan 502`` rows (no ``Planting ID``)."""
    crop_variety = nursery_sheet_crop_variety_cell(row)
    plan_week_raw = (row.get("Plan Field Week") or "").strip()
    if not crop_variety or not plan_week_raw:
        return None

    plan_year_raw = (row.get("Plan Field Year") or "").strip()
    plan_year = int(str(plan_year_raw).strip()) if plan_year_raw else year
    try:
        plan_week = int(str(plan_week_raw).strip())
    except ValueError:
        return None
    if plan_week <= 0:
        return None

    block_name = (row.get("Block") or "").strip()
    bed_raw = (row.get("Bed") or "").strip()
    if block_name and bed_raw:
        synthetic = {
            "Crop // Variety": crop_variety,
            "Block": block_name,
            "Bed": bed_raw,
            "Plan Field Year": str(plan_year),
            "Plan Field Week": str(plan_week),
        }
        return resolve_field_walk_planting_from_context(synthetic, year)

    crop_name, variety = split_crop_variety_for_nursery_sheet(crop_variety)
    if not crop_name:
        return None
    crop = CropInfo.objects.filter(name__iexact=crop_name).first()
    if not crop:
        return None

    candidates = (
        Planting.objects.filter(planning_year__year=plan_year, crop=crop)
        .select_related("crop", "block", "planning_year")
        .order_by("id")
    )
    if variety:
        candidates = candidates.filter(variety__iexact=variety)

    matches = []
    for planting in candidates:
        iso_year, iso_week, _ = planting.planned_plant_date.isocalendar()
        if iso_year == plan_year and iso_week == plan_week:
            matches.append(planting)

    if len(matches) == 1:
        return matches[0]
    return None


def derived_nursery_seed_date(planting: Planting) -> date | None:
    if planting.crop.nursery_weeks == 0:
        return None
    return planting.planned_plant_date - timedelta(weeks=planting.crop.nursery_weeks)


def derived_nursery_pot_up_date(planting: Planting) -> date | None:
    seed = derived_nursery_seed_date(planting)
    if seed is None or not planting.crop.weeks_until_pot_up:
        return None
    return seed + timedelta(weeks=planting.crop.weeks_until_pot_up)


def int_from_tray_size_cell(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("na", "n/a", "-", "—"):
        return None
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        v = int(m.group(0))
        return v if v > 0 else None
    except ValueError:
        return None
