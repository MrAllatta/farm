"""Staff-facing channel and product scope (LIVE-2 / LIVE-6 / LIVE-7).

Outlet pickers and weekly workflows use real sales channels. Importer-seeded
``Markets (annual plan)`` / ``Orders (annual plan)`` / ``CSA (annual plan)``
pseudo-channels (301 annual grids) are excluded so they do not appear next to
operator outlets or steal default ``first channel`` selection.

Product pickers for in-season work prefer ``CropSalesFormat`` rows for crops
that have non-excluded plantings in the active planning year — same rule as
``sales.views`` / weekly order (LIVE-2). :func:`resolve_live2_scope` is the shared
planning-year + ISO-week harvest-overlap classifier; pass ``live2_scope=`` into
:func:`active_crop_sales_formats_for_planning_year` when the caller already resolved
scope to avoid duplicate planting queries.
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class Live2Scope:
    """LIVE-2 / LIVE-7: how weekly order and pickers narrow ``CropSalesFormat`` rows."""

    scope: str  # full_catalog | crop_plan_week | crop_plan_inexact_week
    year_crop_ids: frozenset[int]
    week_overlap_crop_ids: frozenset[int]


def resolve_live2_scope(year_obj, iso_week: int | None) -> Live2Scope:
    """Single source of truth for crop-plan vs ISO-week harvest overlap (LIVE-2).

    ``scope`` matches ``WeeklyChannelOrderView`` / ``weekly_order_surface_hints``:
    ``full_catalog`` (no in-year plantings), ``crop_plan_week`` (harvest window hits week),
    ``crop_plan_inexact_week`` (in-year plantings but none overlap this ISO week).
    """
    empty: frozenset[int] = frozenset()
    if not year_obj:
        return Live2Scope("full_catalog", empty, empty)
    year_crop_ids = frozenset(
        Planting.objects.filter(planning_year=year_obj)
        .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
        .values_list("crop_id", flat=True)
        .distinct()
    )
    if not year_crop_ids:
        return Live2Scope("full_catalog", empty, empty)
    if iso_week is None:
        return Live2Scope("crop_plan_inexact_week", year_crop_ids, empty)
    wn = max(1, min(52, int(iso_week)))
    week_monday, week_sunday = week_bounds_for_planning_year(year_obj.year, wn)
    overlap = frozenset(
        Planting.objects.filter(planning_year=year_obj)
        .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
        .filter(
            planned_first_harvest_date__lte=week_sunday,
            planned_last_harvest_date__gte=week_monday,
        )
        .values_list("crop_id", flat=True)
        .distinct()
    )
    if overlap:
        return Live2Scope("crop_plan_week", year_crop_ids, overlap)
    return Live2Scope("crop_plan_inexact_week", year_crop_ids, overlap)


def active_crop_sales_formats_for_planning_year(
    year_obj, *, iso_week: int | None = None, live2_scope: Live2Scope | None = None
):
    """LIVE-2: products tied to crops with plantings in the active year (when set).

    When ``iso_week`` is set, prefer crops whose harvest window overlaps that ISO week’s
    Monday–Sunday window (same rule as weekly order); if none overlap, fall back to all
    in-year crops (off-season / shoulder week).

    Pass ``live2_scope`` when the caller already computed :func:`resolve_live2_scope` to
    avoid duplicate planting queries (e.g. weekly channel order).
    """
    products = (
        CropSalesFormat.objects.filter(is_active=True)
        .select_related("crop")
        .order_by("crop__crop_type", "crop__name", "product_name")
    )
    if not year_obj:
        return products
    meta = live2_scope or resolve_live2_scope(year_obj, iso_week)
    if meta.scope == "full_catalog":
        return products
    if meta.scope == "crop_plan_week":
        return products.filter(crop_id__in=meta.week_overlap_crop_ids)
    return products.filter(crop_id__in=meta.year_crop_ids)


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
