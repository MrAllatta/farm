"""LIVE-17 (C): deterministic rules for planting PlanningYear vs bundle folder year.

Default importer behavior (folder wins) does not import this module for binding;
``import_historical_data`` uses it only when ``--plantings-planning-year-from-planned-date``
is enabled.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

BindingDecision = Literal[
    "folder_default",
    "date_rebind_same_year",
    "date_rebind_shoulder",
    "date_rebind_forced_past_threshold",
    "folder_wins_far_without_force",
]


def planting_target_planning_calendar_year(
    *,
    folder_year: int,
    planned_date_calendar_year: int,
    planning_year_from_planned_date: bool,
    force_planned_date_past_threshold: bool,
) -> tuple[int, BindingDecision]:
    """Return (calendar year used to resolve ``PlanningYear``, decision tag).

    Threshold matches LIVE-8 / LIVE-17(B): "far" means ``abs(D - folder_year) > 1``.

    - Default (``planning_year_from_planned_date`` false): always ``folder_year``.
    - Opt-in: if ``abs(D - folder_year) <= 1``, target is ``D`` (shoulder / overlap).
    - If ``abs(D - folder_year) > 1`` and ``force_planned_date_past_threshold``, target is ``D``.
    - Otherwise folder wins (same as legacy) and caller should emit
      ``planting_date_year_mismatch`` when ``abs(D - folder_year) > 1``.
    """
    d = planned_date_calendar_year
    y = folder_year
    if not planning_year_from_planned_date:
        return y, "folder_default"
    if abs(d - y) <= 1:
        if d == y:
            return d, "date_rebind_same_year"
        return d, "date_rebind_shoulder"
    if force_planned_date_past_threshold:
        return d, "date_rebind_forced_past_threshold"
    return y, "folder_wins_far_without_force"


def parse_planned_plant_calendar_year(date_str: str) -> int | None:
    """Return calendar year of *date_str* or None if blank / unparseable.

    Uses the same format list as ``import_historical_data._parse_date_loose`` so
    staging tool (LIVE-17 A) and importer agree.
    """
    if date_str is None:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().year
        except ValueError:
            continue
    return None


def parse_planned_plant_date(date_str: str) -> date | None:
    """Parse *date_str* to a ``date`` or None if blank / unparseable."""
    if date_str is None:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
