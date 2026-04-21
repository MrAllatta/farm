"""Cascade field-walk yield adjustments into planned harvest quantities."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from planning.models import HarvestEvent, Planting


def apply_yield_adjustment_to_future_harvests(
    planting: Planting,
    yield_adjust_pct: int,
    *,
    from_date: date | None = None,
) -> int:
    """
    Scale planned_quantity on future, not-yet-harvested events by yield_adjust_pct/100.

    Returns count of HarvestEvent rows updated.
    """
    if yield_adjust_pct is None or yield_adjust_pct == 100:
        return 0

    start = from_date or date.today()
    factor = Decimal(yield_adjust_pct) / Decimal(100)

    qs = planting.harvest_events.filter(
        planned_date__gte=start,
        actual_quantity__isnull=True,
    )
    updated = 0
    for he in qs:
        he.planned_quantity = (he.planned_quantity * factor).quantize(Decimal("0.01"))
        he.save(update_fields=["planned_quantity"])
        updated += 1
    return updated
