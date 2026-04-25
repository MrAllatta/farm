"""Human-facing planting identifiers and schedule chips for operations surfaces."""

from __future__ import annotations

from datetime import date


def planting_variety_display(planting) -> str:
    """Prefer catalog ``Variety``; fall back to free-text ``Planting.variety``."""
    vo = getattr(planting, "variety_obj", None)
    if vo is not None:
        name = (getattr(vo, "name", None) or "").strip()
        if name:
            return name
    return (getattr(planting, "variety", None) or "").strip()


def format_planting_display_id(pk: int) -> str:
    """Legacy ops label: integer PK with ``P-`` prefix and zero-padded width.

    Prefer :func:`planting_unit_code` for anything user-facing after
    ``planting_code`` exists on the model.
    """
    return f"P-{int(pk):05d}"


def planting_unit_code(planting) -> str:
    """Durable planting unit code (e.g. ``P-2026-0001``), or legacy PK label."""
    code = (getattr(planting, "planting_code", None) or "").strip()
    if code:
        return code
    pk = getattr(planting, "pk", None)
    if pk:
        return format_planting_display_id(int(pk))
    return ""


def planting_unit_primary_label(planting) -> str:
    """Primary line: crop name and optional variety."""
    crop = getattr(planting, "crop", None)
    name = crop.name if crop else "—"
    v = planting_variety_display(planting)
    return f"{name} — {v}" if v else name


def planting_unit_matrix_sublabel(planting) -> str:
    """Matrix bar second line: code, bed range, plant ISO week."""
    code = planting_unit_code(planting)
    pw = planting.planned_plant_date.isocalendar()[1]
    return f"{code} · b{planting.bed_start}-{planting.bed_end} · plant wk {pw}"


def planting_unit_full_label(planting) -> str:
    """Long single-line summary for tooltips and dense print."""
    crop = getattr(planting, "crop", None)
    name = crop.name if crop else "—"
    v = planting_variety_display(planting)
    block = getattr(planting, "block", None)
    bname = block.name if block else "—"
    pw = planting.planned_plant_date.isocalendar()[1]
    code = planting_unit_code(planting)
    crop_part = f"{name} — {v}" if v else name
    return f"{code} · {crop_part} · {bname} b{planting.bed_start}-{planting.bed_end} · plant wk {pw}"


def planting_map_segment_label(planting) -> str:
    """503 high-level segment anchor text: code + short crop (+ variety) + beds."""
    crop = getattr(planting, "crop", None)
    name = (crop.name if crop else "—")[:18]
    v = planting_variety_display(planting)
    crop_part = f"{name} — {v[:12]}" if v else name
    return f"{planting_unit_code(planting)} · {crop_part} b{planting.bed_start}-{planting.bed_end}"


def planting_schedule_chip_css_class(
    planned_plant_date: date,
    actual_plant_date: date | None,
    today: date,
) -> str:
    """
    Derive chip class from planned plant date vs actual plant date, else today.

    - ``chip-plant-schedule-behind``: actual or (if not planted yet) calendar
      date is after the planned plant date.
    - ``chip-plant-schedule-ahead``: actual plant date is before planned
      (planted early).
    - ``chip-plant-schedule-on``: on time, or not yet due (today <= planned and
      no actual).
    """
    if actual_plant_date is not None:
        ref = actual_plant_date
    else:
        ref = today
    if ref > planned_plant_date:
        return "chip-plant-schedule-behind"
    if actual_plant_date is not None and ref < planned_plant_date:
        return "chip-plant-schedule-ahead"
    return "chip-plant-schedule-on"
