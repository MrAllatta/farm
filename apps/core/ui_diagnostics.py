"""Structured empty-state / diagnostic hints for staff-facing surfaces.

Copy here is stable so route tests can assert on ``id`` keys or short titles.
"""

from __future__ import annotations


def harvest_surface_hints(
    *,
    week_harvest_event_count: int,
    planting_count_excl_dead: int,
    plantings_missing_harvest_events: int,
) -> list[dict[str, str]]:
    """Harvest needs / weekly harvest entry: explain empty or misleading weeks."""
    out: list[dict[str, str]] = []
    if planting_count_excl_dead == 0:
        out.append(
            {
                "id": "no_plantings",
                "title": "No plantings for this planning year",
                "detail": (
                    "There are no plantings yet (excluding skipped/failed). "
                    "Add plantings in the crop planner or switch the planning-year focus in the header."
                ),
            }
        )
        return out
    if plantings_missing_harvest_events > 0:
        out.append(
            {
                "id": "missing_generated_harvests",
                "title": f"{plantings_missing_harvest_events} planting(s) lack harvest events",
                "detail": (
                    "Harvest needs and sales-plan supply use generated harvest events. "
                    "After import, run the repair step (management command: repair_planting_events) "
                    "so pending picks exist without duplicating recorded harvests."
                ),
            }
        )
    if week_harvest_event_count == 0:
        out.append(
            {
                "id": "no_events_this_week",
                "title": "No harvest events this ISO week",
                "detail": (
                    "Nothing is scheduled for this week. Try another week or check each planting's "
                    "first/last harvest window."
                ),
            }
        )
    return out


def dashboard_surface_hints(
    *,
    harvest_events_this_week: int,
    active_planting_count: int,
    plantings_missing_harvest_events: int,
    week_sales_plan_target: float | int,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if active_planting_count == 0:
        out.append(
            {
                "id": "dash_no_active_plantings",
                "title": "No active plantings",
                "detail": "Nothing in planted / growing / harvesting — open the crop planner or roll the season forward.",
            }
        )
    if plantings_missing_harvest_events > 0:
        out.append(
            {
                "id": "dash_missing_harvest_events",
                "title": f"{plantings_missing_harvest_events} planting(s) still need harvest events",
                "detail": "Repair planting events after import so this week's harvest counts stay meaningful.",
            }
        )
    if harvest_events_this_week == 0 and active_planting_count > 0:
        out.append(
            {
                "id": "dash_no_harvest_this_week",
                "title": "No harvests scheduled this calendar week",
                "detail": "This may be normal off-season, or harvest windows may not overlap the current ISO week.",
            }
        )
    if not week_sales_plan_target:
        out.append(
            {
                "id": "dash_no_week_sales_target",
                "title": "No sales-plan target for this week",
                "detail": (
                    "Active SalesPlanBucket rows do not cover this ISO week — import 302 sales plan / buckets "
                    "or adjust bucket week ranges."
                ),
            }
        )
    return out


def sales_plan_surface_hints(
    *,
    product_count: int,
    rollup_category_ok: bool,
    harvest_event_year_total: int,
    plantings_missing_harvest_events: int,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not rollup_category_ok:
        out.append(
            {
                "id": "sales_plan_no_category",
                "title": "Sales category / rollup not ready",
                "detail": "Import reference categories (Markets / Orders / CSA) or the 302 rollup plan.",
            }
        )
    if product_count == 0:
        out.append(
            {
                "id": "sales_plan_no_products",
                "title": "No active sales products",
                "detail": "Enable CropSalesFormat rows (reference import) before demand entry.",
            }
        )
    if harvest_event_year_total == 0 and product_count:
        out.append(
            {
                "id": "sales_plan_no_harvest_supply",
                "title": "No harvest events this year — supply reads as zero",
                "detail": (
                    "Shortage marks compare demand to harvest supply. Add plantings with harvest events "
                    "or run repair_planting_events after import."
                ),
            }
        )
    elif plantings_missing_harvest_events > 0 and product_count:
        out.append(
            {
                "id": "sales_plan_partial_supply",
                "title": f"{plantings_missing_harvest_events} planting(s) lack harvest events",
                "detail": "Supply columns may under-report until harvest events exist for those plantings.",
            }
        )
    return out


def nursery_surface_hints(
    *,
    planting_count_excl_dead: int,
    nursery_event_year_total: int,
    window_total_events: int,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if planting_count_excl_dead == 0:
        out.append(
            {
                "id": "nursery_no_plantings",
                "title": "No plantings for this year",
                "detail": "Nursery tasks come from plantings — add plan rows first.",
            }
        )
        return out
    if nursery_event_year_total == 0:
        out.append(
            {
                "id": "nursery_no_events_year",
                "title": "No nursery events this planning year",
                "detail": (
                    "Nothing to show in seed / pot-up / transplant lists. "
                    "Import nursery rows or run repair for plantings that need a nursery start."
                ),
            }
        )
    elif window_total_events == 0:
        out.append(
            {
                "id": "nursery_empty_window",
                "title": "No nursery work in this 4-week window",
                "detail": "Shift the week focus or check planting dates — events may fall outside this slice.",
            }
        )
    return out


def market_entry_surface_hints(
    *,
    has_pack_list: bool,
    planning_year_label: str,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not has_pack_list:
        out.append(
            {
                "id": "market_no_pack_allocations",
                "title": "No pack allocations for this channel and date",
                "detail": (
                    f"The detailed grid is using every active product as a template. "
                    f"Add PackAllocation rows for {planning_year_label} so “brought” defaults match what left the pack shed."
                ),
            }
        )
    return out
