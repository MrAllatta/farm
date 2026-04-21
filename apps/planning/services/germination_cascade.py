"""Scale future planned harvest quantities when germination is below target."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from planning.models import HarvestEvent, NurseryEvent, Planting

GERMINATION_THRESHOLD_PCT = Decimal("70.00")


def apply_germination_cascade(planting: Planting, *, from_date: date | None = None) -> int:
    """
    If the latest completed seed nursery event has actual_germination_rate below threshold,
    scale planned_quantity on future, not-yet-harvested events by (rate / threshold).

    Returns count of HarvestEvent rows updated.
    """
    seed_events = (
        NurseryEvent.objects.filter(planting=planting, event_type="seed")
        .exclude(actual_date__isnull=True)
        .exclude(actual_germination_rate__isnull=True)
        .order_by("-actual_date", "-id")
    )
    latest = seed_events.first()
    if latest is None:
        return 0

    rate = latest.actual_germination_rate
    if rate is None or rate >= GERMINATION_THRESHOLD_PCT:
        return 0

    factor = rate / GERMINATION_THRESHOLD_PCT
    start = from_date or date.today()

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
