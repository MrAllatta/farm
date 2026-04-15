"""Shared planning-year resolution helpers for app surfaces."""

from planning.models import PlanningYear


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
