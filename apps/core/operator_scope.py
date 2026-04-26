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
from operations.services.week_ops import EXCLUDED_PLANTING_STATUSES
from planning.models import Planting


def operator_sales_channels():
    """Real outlet channels: exclude 301 annual-plan rollup rows."""
    return SalesChannel.objects.exclude(name__in=ROLLUP_PLAN_CHANNEL_NAMES).order_by(
        "allocation_priority", "name"
    )


def first_operator_sales_channel():
    """Default channel for nav links and harvest-needs handoff (not a rollup)."""
    return operator_sales_channels().order_by("allocation_priority", "id").first()


def active_crop_sales_formats_for_planning_year(year_obj):
    """LIVE-2: products tied to crops with plantings in the active year (when set)."""
    products = (
        CropSalesFormat.objects.filter(is_active=True)
        .select_related("crop")
        .order_by("crop__crop_type", "crop__name")
    )
    if not year_obj:
        return products
    crop_ids = list(
        Planting.objects.filter(planning_year=year_obj)
        .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
        .values_list("crop_id", flat=True)
        .distinct()
    )
    if crop_ids:
        products = products.filter(crop_id__in=crop_ids)
    return products


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
