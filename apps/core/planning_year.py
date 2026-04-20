"""Shared planning-year resolution helpers for app surfaces."""

from __future__ import annotations

from datetime import date

from django.http import HttpRequest

from planning.models import PlanningYear

# Session key for UI planning focus (this calendar year vs next in scope).
PLANNING_YEAR_SESSION_KEY = "planning_year_id"


def operational_anchor_year(today: date | None = None) -> int:
    """Calendar year used as the operational 'now' for default planning scope.

    Season boundaries (e.g. rollover month) can replace calendar-year logic later
    without changing call sites.
    """
    return (today or date.today()).year


def planning_years_in_clock_scope(
    anchor: int | None = None,
) -> list[PlanningYear]:
    """PlanningYear rows for ``anchor`` and ``anchor + 1`` (this / next scope)."""
    y = operational_anchor_year() if anchor is None else anchor
    return list(PlanningYear.objects.filter(year__in=(y, y + 1)).order_by("year"))


def resolve_current_planning_year(status_priority=("active", "planning"), fallback_latest=False):
    """Return the most relevant planning year based on explicit status priority."""
    base_qs = PlanningYear.objects.all()

    for status in status_priority:
        match = base_qs.filter(status=status).order_by("-year").first()
        if match:
            return match

    if fallback_latest:
        return base_qs.order_by("-year").first()

    return None


def get_effective_planning_year(
    request: HttpRequest | None,
    *,
    today: date | None = None,
) -> PlanningYear | None:
    """Planning bundle the web UI should use for the current request.

    - Prefers ``request.session[PLANNING_YEAR_SESSION_KEY]`` when it points at a
      ``PlanningYear`` whose ``year`` is ``operational_anchor_year()`` or the next year.
    - Otherwise prefers ``resolve_current_planning_year()`` when that row is in the same scope.
    - Otherwise defaults to the anchor calendar year's row if present, else the next year.

    When no ``PlanningYear`` exists for the two-year clock window, falls back to
    ``resolve_current_planning_year()`` so legacy databases and management commands
    keep working (callers without a request should keep using that function).
    """
    today = today or date.today()
    anchor = operational_anchor_year(today)
    in_scope = PlanningYear.objects.filter(year__in=(anchor, anchor + 1)).order_by("year")

    if not in_scope.exists():
        return resolve_current_planning_year()

    if request is not None:
        raw_id = request.session.get(PLANNING_YEAR_SESSION_KEY)
        if raw_id is not None:
            try:
                pk = int(raw_id)
            except (TypeError, ValueError):
                pk = None
            if pk is not None:
                selected = in_scope.filter(pk=pk).first()
                if selected is not None:
                    return selected

    resolved = resolve_current_planning_year()
    if resolved is not None and in_scope.filter(pk=resolved.pk).exists():
        return resolved

    preferred = in_scope.filter(year=anchor).first()
    if preferred is not None:
        return preferred
    return in_scope.filter(year=anchor + 1).first()


def set_session_planning_year(request: HttpRequest, planning_year: PlanningYear) -> None:
    request.session[PLANNING_YEAR_SESSION_KEY] = planning_year.id
    request.session.modified = True
