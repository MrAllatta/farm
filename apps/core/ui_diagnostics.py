"""Structured empty-state / diagnostic hints for staff-facing surfaces.

Copy here is stable so route tests can assert on ``id`` keys or short titles.
"""

from __future__ import annotations


def harvest_surface_hints(
    *,
    week_harvest_event_count: int,
    planting_count_excl_dead: int,
    plantings_missing_harvest_events: int,
    weekly_sales_demand_count: int = 0,
    planning_year_id: int | None = None,
    planning_calendar_year: int | None = None,
) -> list[dict[str, str]]:
    """Harvest needs / weekly harvest entry: explain empty or misleading weeks."""
    out: list[dict[str, str]] = []

    def _repair_cmd_year() -> str:
        if planning_calendar_year is not None:
            return f'make manage ARGS="repair_planting_events --year {planning_calendar_year}"'
        return 'make manage ARGS="repair_planting_events --year <calendar year>"'

    def _repair_cmd_pyid() -> str:
        if planning_year_id is not None:
            return f'make manage ARGS="repair_planting_events --planning-year-id {planning_year_id}"'
        return 'make manage ARGS="repair_planting_events --planning-year-id <id>"'

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
        repair_lines = (
            f"This list is built from generated HarvestEvent rows. After import, from the repo root run "
            f"{_repair_cmd_pyid()} (scoped to this planning year), or {_repair_cmd_year()} "
            "when several years need repair. The command only adds missing rows; it does not overwrite picks "
            "that already have recorded harvest quantities."
        )
        out.append(
            {
                "id": "missing_generated_harvests",
                "title": f"{plantings_missing_harvest_events} planting(s) lack harvest events",
                "detail": repair_lines,
            }
        )
    if week_harvest_event_count == 0:
        if weekly_sales_demand_count > 0:
            demand_detail = (
                "Saved weekly channel orders flow here as committed demand. "
                "This ISO week has planned sales lines, but no harvest events dated in this week, "
                "so this screen can look empty even though the sales plan has numbers."
            )
            if plantings_missing_harvest_events > 0:
                demand_detail += (
                    " If plantings are missing generated events entirely, run the repair command above first; "
                    "otherwise check the week selector or each planting's harvest window."
                )
            else:
                demand_detail += (
                    " Confirm the ISO week, planning year in the header, and harvest windows; "
                    "if imports skipped event generation, use the same repair command as for missing harvest rows."
                )
            out.append(
                {
                    "id": "sales_demand_without_harvest_supply",
                    "title": "Weekly sales/order demand exists, but no harvest supply is scheduled",
                    "detail": demand_detail,
                }
            )
        if plantings_missing_harvest_events == 0 and planting_count_excl_dead > 0:
            out.append(
                {
                    "id": "no_harvest_events_this_iso_week",
                    "title": "No harvest picks fall in this ISO week",
                    "detail": (
                        "Plantings have harvest events, but none use dates in this week. "
                        "Confirm the planning year in the header matches the season you imported, "
                        "then try another ISO week or widen each planting's first/last harvest window."
                    ),
                }
            )
    if (
        week_harvest_event_count > 0
        and weekly_sales_demand_count == 0
        and planting_count_excl_dead > 0
    ):
        out.append(
            {
                "id": "no_committed_sales_demand_week",
                "title": "No weekly sales (PLAN) demand matched this ISO week for these crops",
                "detail": (
                    "Harvest picks exist, but this week’s aggregated channel plan lines are empty — "
                    "open a weekly channel order for this ISO week, save quantities, or confirm the "
                    "sales plan / channel mix for the active planning year."
                ),
            }
        )
    return out


def crop_plan_product_scope_hints(weekly_order_products_scope: str) -> list[dict[str, str]]:
    """LIVE-7: shared copy for weekly order (and staff explanations elsewhere).

    ``scope`` matches ``WeeklyChannelOrderView._weekly_order_crop_sales_formats``:
    ``full_catalog`` | ``crop_plan_week`` | ``crop_plan_inexact_week``.
    """
    if weekly_order_products_scope == "crop_plan_inexact_week":
        return [
            {
                "id": "weekly_order_products_not_this_iso_week",
                "title": "Product list is limited to the crop plan, not this ISO week’s harvest window",
                "detail": (
                    "No plantings have a harvest window overlapping this ISO week, so every in-plan crop "
                    "is still shown for ordering. This is a scope filter (off-season or shoulder week), not a "
                    "missing-data bug — switch weeks or extend harvest dates if the list should narrow further."
                ),
            }
        ]
    return []


def sales_plan_product_scope_explanations(*, has_in_plan_plantings: bool) -> list[dict[str, str]]:
    """Sales plan: why the product list width differs from a single ISO week (LIVE-2 + LIVE-7)."""
    out: list[dict[str, str]] = []
    if has_in_plan_plantings:
        out.append(
            {
                "id": "sales_plan_products_limited_to_crop_plan",
                "title": "Product rows match the active crop plan",
                "detail": (
                    "Only crops with at least one in-scope planting in this planning year appear — the same product "
                    "scope as the weekly channel order, pack prep, and inventory pickers. This grid is for the full "
                    "calendar year; it is not ISO-week–narrowed like the weekly order screen."
                ),
            }
        )
    else:
        out.append(
            {
                "id": "sales_plan_products_full_until_plantings",
                "title": "No plantings in this year yet — full product catalog",
                "detail": (
                    "Once the crop plan has plantings, this grid will narrow to those crops’ products. "
                    "Add plantings in the crop planner, or use season init / rollover if you are starting a new year."
                ),
            }
        )
    out.append(
        {
            "id": "sales_plan_iso_week_explanation",
            "title": "Off-season “long” weekly lists use the same scope rule as this grid",
            "detail": (
                "On the weekly channel order, when no harvest window overlaps the selected ISO week, the staff "
                "hint “Product list is limited to the crop plan, not this ISO week’s harvest window” appears — that "
                "is expected scope, not a silent bug. This annual view always lists the year’s planned product rows."
            ),
        }
    )
    return out


def weekly_order_surface_hints(
    *,
    plan_raw_week: int,
    plan_visible_week: int,
    namesake_actuals_on_other_channel: bool,
    channel_name: str,
    has_any_historical: bool,
    historical_name_fallback: bool = False,
    positive_week_demand_products: int = 0,
    weekly_order_products_scope: str = "full_catalog",
    duplicate_channel_name_detected: bool = False,
) -> list[dict[str, str]]:
    """Weekly channel order: demand rollup, historical joins, supply (pairs with LIVE-1/3/4)."""
    out: list[dict[str, str]] = []
    if plan_raw_week > 0 and plan_visible_week == 0:
        out.append(
            {
                "id": "all_channel_demand_shadowed",
                "title": "All-channel demand shows zero, but plan rows exist for this week",
                "detail": (
                    "Outlet-level weekly plan lines are hiding duplicate annual rollup rows for the same "
                    "product / category / ISO week. Demand totals only count the visible slice — "
                    "confirm lines on real sales channels or adjust the sales plan import."
                ),
            }
        )
    elif plan_raw_week > plan_visible_week > 0:
        out.append(
            {
                "id": "all_channel_demand_partially_shadowed",
                "title": "Some plan rows are hidden from the all-channel demand total",
                "detail": (
                    f"{plan_raw_week - plan_visible_week} duplicate rollup plan row(s) for this ISO week "
                    "were dropped because outlet-level plan lines own the same category slice."
                ),
            }
        )
    if plan_raw_week == 0 and positive_week_demand_products == 0:
        out.append(
            {
                "id": "no_plan_rows_this_iso_week",
                "title": "No saved weekly plan (PLAN) rows for this ISO week",
                "detail": (
                    "All-channel demand sums PLAN lines for the active planning year in this ISO week. "
                    "If you expected totals, save quantities on weekly channel orders for this week, "
                    "or import annual product_week_plan lines for this year."
                ),
            }
        )
    elif plan_visible_week > 0 and positive_week_demand_products == 0:
        out.append(
            {
                "id": "plan_rows_zero_all_channel_demand",
                "title": "Plan rows exist but all-channel demand is still zero",
                "detail": (
                    "After rollup de-duplication, PLAN rows are present for this week but every "
                    "rolled-up quantity is zero or unset. Enter positive planned quantities on outlet "
                    "channels, or confirm which channel owns the visible plan slice."
                ),
            }
        )
    out.extend(crop_plan_product_scope_hints(weekly_order_products_scope))
    if namesake_actuals_on_other_channel and not has_any_historical:
        out.append(
            {
                "id": "historical_channel_id_mismatch",
                "title": f"Prior-year actuals may exist on a different “{channel_name}” channel row",
                "detail": (
                    "Historical 601 imports sometimes attach ACTUAL sales to a channel record that is not "
                    "the one in this URL. Reconcile SalesChannel ids in import data, or pick the channel "
                    "that owns the imported history."
                ),
            }
        )
    if historical_name_fallback:
        out.append(
            {
                "id": "historical_from_namesake_channel",
                "title": f"Prior-year cells use another “{channel_name}” channel id",
                "detail": (
                    "At least one calendar year had no ACTUAL rows on this URL’s SalesChannel, so the grid "
                    "fell back to same-name channel rows (typical after 601 re-import). Prefer reconciling "
                    "channel ids in data so history attaches to the channel you use for weekly orders."
                ),
            }
        )
    if duplicate_channel_name_detected:
        out.append(
            {
                "id": "duplicate_channel_rows_same_name",
                "title": f"Multiple SalesChannel rows share the name “{channel_name}”",
                "detail": (
                    "Weekly order joins by this URL channel id first, so duplicate same-name rows can "
                    "split historical cells or saved lines across ids. Reconcile channel ids in import data "
                    "to keep planning and actual history on one canonical channel row."
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
                "detail": (
                    "Run make manage ARGS=\"repair_planting_events --planning-year-id <id>\" after import "
                    "so this week's harvest counts stay meaningful."
                ),
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


def inventory_surface_hints(
    *,
    outside_plan_crop_names: list[str],
    planning_year_label: str = "",
    in_plan_not_field_week_overlap_names: list[str] | None = None,
    field_week_label: str = "",
) -> list[dict[str, str]]:
    """Inventory dashboard: call out carry-over or demo balances (LIVE-7) and in-plan off–field-week rows."""
    out: list[dict[str, str]] = []
    in_plan_not_field_week_overlap_names = in_plan_not_field_week_overlap_names or []
    if outside_plan_crop_names and planning_year_label:
        preview = ", ".join(outside_plan_crop_names[:8])
        if len(outside_plan_crop_names) > 8:
            preview += f", +{len(outside_plan_crop_names) - 8} more"
        out.append(
            {
                "id": "inventory_balance_outside_active_crop_plan",
                "title": "Some inventory crops are not in the active crop plan",
                "detail": (
                    f"Positive balances for: {preview}. "
                    f"They have no {planning_year_label} planting in planned/growing/harvesting — "
                    "usually prior-season carry-over, a demo product, or a ledger-only crop. "
                    "Balances are still real until you adjust them."
                ),
            }
        )
    if in_plan_not_field_week_overlap_names and planning_year_label:
        preview = ", ".join(in_plan_not_field_week_overlap_names[:8])
        if len(in_plan_not_field_week_overlap_names) > 8:
            preview += f", +{len(in_plan_not_field_week_overlap_names) - 8} more"
        wk = field_week_label or "this field ISO week"
        out.append(
            {
                "id": "inventory_in_plan_not_this_field_week",
                "title": "Some balances are in-plan but not in this week’s harvest window",
                "detail": (
                    f"Positive balances for: {preview}. These crops have a {planning_year_label} planting, "
                    f"but no harvest window overlaps {wk} — same idea as the weekly order hint “Product list is "
                    "limited to the crop plan, not this ISO week’s harvest window” (off-season or shoulder). "
                    "Storage, carry-over, and earlier weeks can still show inventory without field harvest this week."
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
                    "or run make manage ARGS=\"repair_planting_events --planning-year-id <id>\" after import."
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
