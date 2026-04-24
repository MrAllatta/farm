"""Human-facing planting identifiers and schedule chips for operations surfaces."""

from __future__ import annotations

from datetime import date


def format_planting_display_id(pk: int) -> str:
    """Stable ops label: integer PK with ``P-`` prefix and zero-padded width."""
    return f"P-{int(pk):05d}"


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
