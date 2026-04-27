"""Staff-facing channel and product scope (LIVE-2 / LIVE-6 / LIVE-7).

Outlet pickers and weekly workflows use real sales channels. Importer-seeded
``Markets (annual plan)`` / ``Orders (annual plan)`` / ``CSA (annual plan)``
pseudo-channels (301 annual grids) are excluded so they do not appear next to
operator outlets or steal default ``first channel`` selection.

Product pickers for in-season work prefer ``CropSalesFormat`` rows for crops
that have non-excluded plantings in the active planning year — same rule as
``sales.views`` / weekly order (LIVE-2).
"""

from __future__ import annotations

from reference.models import CropInfo, CropSalesFormat, SalesChannel
from reference.sales_rollups import ROLLUP_PLAN_CHANNEL_NAMES
from operations.services.week_ops import EXCLUDED_PLANTING_STATUSES, week_bounds_for_planning_year
from planning.models import Planting


def operator_sales_channels():
    """Real outlet channels: exclude 301 annual-plan rollup rows."""
    return SalesChannel.objects.exclude(name__in=ROLLUP_PLAN_CHANNEL_NAMES).order_by(
        "allocation_priority", "name"
    )


def first_operator_sales_channel():
    """Default channel for nav links and harvest-needs handoff (not a rollup)."""
    return operator_sales_channels().order_by("allocation_priority", "id").first()


def active_crop_sales_formats_for_planning_year(year_obj, *, iso_week: int | None = None):
    """LIVE-2: products tied to crops with plantings in the active year (when set).

    When ``iso_week`` is set, prefer crops whose harvest window overlaps that ISO week’s
    Monday–Sunday window (same rule as weekly order); if none overlap, fall back to all
    in-year crops (off-season / shoulder week).
    """
    products = (
        CropSalesFormat.objects.filter(is_active=True)
        .select_related("crop")
        .order_by("crop__crop_type", "crop__name", "product_name")
    )
    if not year_obj:
        return products
    year_crop_ids = list(
        Planting.objects.filter(planning_year=year_obj)
        .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
        .values_list("crop_id", flat=True)
        .distinct()
    )
    if not year_crop_ids:
        return products
    if iso_week is not None:
        wn = max(1, min(52, int(iso_week)))
        week_monday, week_sunday = week_bounds_for_planning_year(year_obj.year, wn)
        week_overlap_crop_ids = list(
            Planting.objects.filter(planning_year=year_obj)
            .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
            .filter(
                planned_first_harvest_date__lte=week_sunday,
                planned_last_harvest_date__gte=week_monday,
            )
            .values_list("crop_id", flat=True)
            .distinct()
        )
        if week_overlap_crop_ids:
            return products.filter(crop_id__in=week_overlap_crop_ids)
        return products.filter(crop_id__in=year_crop_ids)
    return products.filter(crop_id__in=year_crop_ids)


def active_crop_info_for_planning_year(year_obj):
    """LIVE-2: reference crops that have non-excluded plantings in the active year (when set).

    Matches the planting-level crop scope used by ``active_crop_sales_formats_for_planning_year``
    (weekly order / pack batch product pickers), but returns ``CropInfo`` for ledger forms.
    """
    qs = CropInfo.objects.all().order_by("fresh_or_storage", "crop_type", "name")
    if not year_obj:
        return qs
    crop_ids = list(
        Planting.objects.filter(planning_year=year_obj)
        .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
        .values_list("crop_id", flat=True)
        .distinct()
    )
    if crop_ids:
        qs = qs.filter(pk__in=crop_ids)
    return qs
