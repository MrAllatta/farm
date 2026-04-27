"""Weekly channel order workflow (SP-5 / SP-6).

Kept in a submodule to keep ``sales.views`` smaller; imported from ``sales.views``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.contrib import messages
from django.db.models import Q, Sum
from django.db.models.functions import ExtractYear
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import TemplateView

from core.operator_scope import (
    active_crop_sales_formats_for_planning_year,
    first_operator_sales_channel,
    operator_sales_channels,
    resolve_live2_scope,
)
from core.planning_year import get_effective_planning_year
from core.ui_diagnostics import harvest_surface_hints, weekly_order_surface_hints
from operations.models import FieldWalkNote
from operations.services.week_ops import EXCLUDED_PLANTING_STATUSES, week_bounds_for_planning_year
from isoweek import Week

from planning.models import HarvestEvent, PlanningYear, Planting
from planning.services.planting_events_repair import count_plantings_missing_harvest_events
from reference.models import CropSalesFormat, SalesChannel
from reference.sales_rollups import ROLLUP_PLAN_CHANNEL_NAMES, plan_events_without_shadowed_rollups, plan_week_iso_counts
from reports.mixins import ReportContextMixin

from .models import SalesEvent


def _prior_year_empty_cell_title(calendar_year: int, empty_reason: str) -> str:
    """LIVE-10: operator-facing hover text for prior-year cells with no ACTUAL (same ISO week)."""
    y = calendar_year
    if empty_reason == "prior_year_cell_empty_neighbors_both_weeks":
        return (
            f"No ACTUAL this ISO week for this product in {y}; adjacent ISO weeks had sales "
            "(see W-1 / W+1 quantities below)."
        )
    if empty_reason == "prior_year_cell_empty_neighbor_prev_week_only":
        return (
            f"No ACTUAL this ISO week for this product in {y}; the prior ISO week (W-1) had sales "
            "for this product."
        )
    if empty_reason == "prior_year_cell_empty_neighbor_next_week_only":
        return (
            f"No ACTUAL this ISO week for this product in {y}; the next ISO week (W+1) had sales "
            "for this product."
        )
    if empty_reason == "prior_year_cell_empty_no_neighbor_week_sales":
        return (
            f"No ACTUAL for this product in W-1, this week, or W+1 for calendar {y} — often not sold "
            "in that window, or that year was not imported, or channel/product keys do not match history."
        )
    return f"No ACTUAL import for this product in ISO week {y}."


def _add_actual_row(by_product: dict[int, SimpleNamespace], product_id: int, row: SalesEvent) -> None:
    """Merge duplicate ACTUAL rows (same product + week window) for LIVE-1."""
    rq = row.actual_quantity
    rr = row.actual_revenue
    cur = by_product.get(product_id)
    if cur is None:
        by_product[product_id] = SimpleNamespace(actual_quantity=rq, actual_revenue=rr)
        return
    if rq is not None:
        base = cur.actual_quantity if cur.actual_quantity is not None else Decimal("0")
        cur.actual_quantity = base + rq
    if rr is not None:
        base_r = cur.actual_revenue if cur.actual_revenue is not None else Decimal("0")
        cur.actual_revenue = base_r + rr


def _merge_historical_from_strict_and_namesake(
    rows_strict: list[SalesEvent],
    namesake_rows: list[SalesEvent],
    channel: SalesChannel,
    products,
) -> dict:
    """Shared LIVE-1 / LIVE-11 merge: strict channel rows, optional namesake, product-key remap."""
    by_product: dict[int, SimpleNamespace] = {}
    for row in rows_strict:
        if row.product_id:
            _add_actual_row(by_product, row.product_id, row)
    used_name_fallback = False
    if not by_product:
        for row in namesake_rows:
            if row.product_id:
                _add_actual_row(by_product, row.product_id, row)
        if by_product and namesake_rows:
            used_name_fallback = True
    used_product_key_fallback = False
    all_hist_rows = list(rows_strict) + list(namesake_rows)
    product_list = list(products)
    for p in product_list:
        if p.id in by_product:
            continue
        matched = False
        for row in all_hist_rows:
            if not row.product_id:
                continue
            op = row.product
            if op.crop_id == p.crop_id and (op.product_name or "") == (p.product_name or ""):
                _add_actual_row(by_product, p.id, row)
                matched = True
        if matched:
            used_product_key_fallback = True
    return {
        "by_product": by_product,
        "empty": not by_product,
        "used_name_fallback": used_name_fallback,
        "used_product_key_fallback": used_product_key_fallback,
    }


def historical_by_product_for_window_from_pool(
    pool: list[SalesEvent],
    channel: SalesChannel,
    products,
    week_start,
    week_end,
) -> dict:
    """Same semantics as ``historical_by_product_for_window`` using pre-fetched ACTUAL rows."""
    rows_in = [r for r in pool if week_start <= r.sale_date <= week_end]
    rows_strict = [r for r in rows_in if r.channel_id == channel.id]
    by_from_strict: dict[int, SimpleNamespace] = {}
    for row in rows_strict:
        if row.product_id:
            _add_actual_row(by_from_strict, row.product_id, row)
    namesake_rows: list[SalesEvent] = []
    if not by_from_strict:
        namesake_rows = [
            r for r in rows_in if r.channel_id != channel.id and r.channel.name == channel.name
        ]
    return _merge_historical_from_strict_and_namesake(rows_strict, namesake_rows, channel, products)


def historical_by_product_for_window(
    channel: SalesChannel,
    products,
    week_start,
    week_end,
) -> dict:
    """LIVE-1 / LIVE-11: ACTUAL rows in [week_start, week_end] keyed to current-grid product ids."""
    pool = list(
        SalesEvent.objects.filter(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            sale_date__gte=week_start,
            sale_date__lte=week_end,
        )
        .filter(Q(channel_id=channel.id) | Q(channel__name=channel.name))
        .select_related("product", "product__crop", "channel")
    )
    return historical_by_product_for_window_from_pool(pool, channel, products, week_start, week_end)


class WeeklyChannelOrderView(ReportContextMixin, TemplateView):
    """One ISO week + channel: planned demand, harvest supply, prior-year actuals, editable plan."""

    template_name = "sales/weekly_channel_order.html"

    def _weekly_order_crop_sales_formats(self, week_num: int):
        """LIVE-2: limit pickers to crops in the crop plan (ISO week overlap when possible)."""
        wn = max(1, min(52, int(week_num)))
        week_monday, week_sunday = week_bounds_for_planning_year(self.year_obj.year, wn)
        meta = resolve_live2_scope(self.year_obj, wn)
        products = active_crop_sales_formats_for_planning_year(
            self.year_obj, iso_week=wn, live2_scope=meta
        )
        return products, week_monday, week_sunday, meta.scope

    def dispatch(self, request, *args, **kwargs):
        self.year_obj = get_effective_planning_year(request)
        if not self.year_obj:
            messages.error(request, "No active planning year configured.")
            return redirect("planning:matrix")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        """Redirect away from 301 annual-plan pseudo-channels (LIVE-6)."""
        ch = get_object_or_404(SalesChannel, pk=kwargs["channel_id"])
        if ch.name in ROLLUP_PLAN_CHANNEL_NAMES:
            alt = first_operator_sales_channel()
            if alt and alt.id != ch.id:
                messages.info(
                    request,
                    "Weekly orders use outlet channels. Switched from an annual-plan rollup row.",
                )
                return redirect(
                    reverse(
                        "sales:weekly_channel_order",
                        kwargs={"channel_id": alt.id, "week": kwargs.get("week")},
                    )
                )
        return super().get(request, *args, **kwargs)

    def post(self, request, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)

        channel = get_object_or_404(SalesChannel, pk=kwargs["channel_id"])
        week_num = self.resolve_week(kwargs.get("week"))
        products, week_monday, week_sunday, _scope = self._weekly_order_crop_sales_formats(week_num)
        updated = 0

        for product in products:
            key = f"qty_{product.id}"
            raw = (request.POST.get(key) or "").strip()
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
                channel=channel,
                product=product,
                sale_date__gte=week_monday,
                sale_date__lte=week_sunday,
            ).delete()
            if not raw:
                continue
            try:
                qty = Decimal(raw)
            except (InvalidOperation, TypeError):
                continue
            if qty <= 0:
                continue
            revenue = qty * (product.sale_price or Decimal("0"))
            SalesEvent.objects.update_or_create(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
                channel=channel,
                product=product,
                sale_date=week_monday,
                defaults={
                    "planned_quantity": qty,
                    "planned_revenue": revenue,
                    "notes": "Weekly order builder",
                },
            )
            updated += 1

        messages.success(request, f"Saved {updated} planned product line(s) for week {week_num}.")
        return redirect(
            reverse(
                "sales:weekly_channel_order",
                kwargs={"channel_id": channel.id, "week": week_num},
            )
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        channel = get_object_or_404(SalesChannel, pk=kwargs["channel_id"])
        week_num = self.resolve_week(kwargs.get("week"))
        cal_year = self.year_obj.year
        products, week_monday, week_sunday, weekly_order_products_scope = (
            self._weekly_order_crop_sales_formats(week_num)
        )

        all_plan = list(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
            ).select_related("product", "channel", "channel__category", "sales_category")
        )
        demand_by_product_week = defaultdict(Decimal)
        demand_by_crop_week = defaultdict(Decimal)
        for row in plan_events_without_shadowed_rollups(all_plan):
            if (
                row.product_id
                and week_monday <= row.sale_date <= week_sunday
            ):
                q = row.planned_quantity or Decimal("0")
                demand_by_product_week[row.product_id] += q
                crop_id = getattr(getattr(row, "product", None), "crop_id", None)
                if crop_id:
                    demand_by_crop_week[crop_id] += q

        plan_raw_week, plan_visible_week = plan_week_iso_counts(
            all_plan,
            week_num,
            week_start=week_monday,
            week_end=week_sunday,
        )
        planting_count_excl_dead = (
            Planting.objects.filter(planning_year=self.year_obj)
            .exclude(status__in=EXCLUDED_PLANTING_STATUSES)
            .count()
        )
        plantings_missing_harvest = count_plantings_missing_harvest_events(self.year_obj.id)
        weekly_demand_row_count = sum(
            1 for q in demand_by_product_week.values() if q and q > Decimal("0")
        )

        harvest_in_week_qs = HarvestEvent.objects.filter(
            planting__planning_year=self.year_obj,
            planned_date__gte=week_monday,
            planned_date__lte=week_sunday,
        ).exclude(planting__status__in=EXCLUDED_PLANTING_STATUSES)

        week_harvest_event_count = harvest_in_week_qs.count()

        harvest_by_crop: dict[int, Decimal] = {}
        for row in harvest_in_week_qs.values("planting__crop_id").annotate(
            t=Sum("planned_quantity")
        ):
            cid = row["planting__crop_id"]
            harvest_by_crop[cid] = row["t"] or Decimal("0")

        harvest_crop_ids = set(harvest_by_crop.keys())

        field_note_by_crop = {}
        if harvest_crop_ids:
            latest_notes = (
                FieldWalkNote.objects.filter(
                    planting__planning_year=self.year_obj,
                    planting__crop_id__in=harvest_crop_ids,
                    walk_date__lte=week_sunday,
                )
                .select_related("planting", "planting__crop")
                .order_by("planting__crop_id", "-walk_date", "-id")
            )
            for note in latest_notes:
                field_note_by_crop.setdefault(note.planting.crop_id, note)

        historical = []
        # LIVE-1 / LIVE-10: prior-year columns are driven by planning years, but must also
        # include any calendar year present in imported ACTUALs (e.g. 2023) even when a
        # PlanningYear row was never created.
        in_planning_table = set(
            PlanningYear.objects.filter(year__lt=cal_year).values_list("year", flat=True)
        )
        actual_years = set(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.ACTUAL,
                sale_date__year__lt=cal_year,
            )
            .annotate(sale_year=ExtractYear("sale_date"))
            .values_list("sale_year", flat=True)
        )
        prior_year_set = in_planning_table | actual_years
        prior_year_ints = sorted(prior_year_set, reverse=True)[:6]
        iso_week_has_prior_actuals_any_channel = False
        for y in prior_year_ints:
            hm_iso, hs_iso = self.week_window(y, week_num)
            if SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.ACTUAL,
                sale_date__gte=hm_iso,
                sale_date__lte=hs_iso,
            ).exists():
                iso_week_has_prior_actuals_any_channel = True
                break
        prev_week_num = self.week_navigation(week_num)["prev_week_num"]
        next_week_num = self.week_navigation(week_num)["next_week_num"]
        historical_prev_by_year = {}
        historical_next_by_year = {}
        for y in prior_year_ints:
            hm, hs = self.week_window(y, week_num)
            block = historical_by_product_for_window(channel, products, hm, hs)
            block["calendar_year"] = y
            block["week_monday"] = hm
            historical.append(block)
            hp_monday, hp_sunday = self.week_window(y, prev_week_num)
            hn_monday, hn_sunday = self.week_window(y, next_week_num)
            historical_prev_by_year[y] = historical_by_product_for_window(
                channel, products, hp_monday, hp_sunday
            )
            historical_next_by_year[y] = historical_by_product_for_window(
                channel, products, hn_monday, hn_sunday
            )

        has_any_historical = any(not h["empty"] for h in historical)
        historical_name_fallback = any(h.get("used_name_fallback") for h in historical)
        historical_product_key_fallback = any(
            h.get("used_product_key_fallback") for h in historical
        )
        namesake_actuals_on_other_channel = False
        duplicate_channel_name_detected = SalesChannel.objects.filter(name=channel.name).exclude(
            id=channel.id
        ).exists()
        if not has_any_historical:
            for y in prior_year_ints:
                hm, hs = self.week_window(y, week_num)
                if (
                    SalesEvent.objects.filter(
                        entry_kind=SalesEvent.EntryKind.ACTUAL,
                        sale_date__gte=hm,
                        sale_date__lte=hs,
                        channel__name=channel.name,
                    )
                    .exclude(channel_id=channel.id)
                    .exists()
                ):
                    namesake_actuals_on_other_channel = True
                    break

        order_rows = []
        for product in products:
            cohort_products = [p for p in products if p.crop_id == product.crop_id]
            planned_rows = SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
                channel=channel,
                product=product,
                sale_date__gte=week_monday,
                sale_date__lte=week_sunday,
            )
            planned_qty = planned_rows.aggregate(t=Sum("planned_quantity"))["t"] or Decimal("0")
            hq = product.harvest_qty_per_sale_unit or Decimal("1")
            if hq <= 0:
                hq = Decimal("1")
            h_units = harvest_by_crop.get(product.crop_id, Decimal("0"))
            supply_sale_units = (h_units / hq).quantize(Decimal("0.01"))
            # LIVE-3: PLAN rows may key to a different CropSalesFormat than this channel's row
            # while sharing the same crop; sum cohort SKUs first, then fall back to crop-wide demand.
            demand_all = demand_by_product_week.get(product.id, Decimal("0"))
            demand_all_channel_note = None
            if demand_all == 0 and len(cohort_products) > 1:
                cohort_sum = sum(
                    demand_by_product_week.get(p.id, Decimal("0")) for p in cohort_products
                )
                if cohort_sum > 0:
                    demand_all = cohort_sum
                    demand_all_channel_note = "sibling_skus"
                else:
                    crop_all = demand_by_crop_week.get(product.crop_id, Decimal("0"))
                    if crop_all > 0:
                        demand_all = crop_all
                        demand_all_channel_note = "crop_all_channels"
            elif demand_all == 0 and len(cohort_products) == 1:
                crop_one = demand_by_crop_week.get(product.crop_id, Decimal("0"))
                if crop_one > 0:
                    demand_all = crop_one
                    demand_all_channel_note = "crop_all_channels"
            shortage = demand_all > supply_sale_units + Decimal("0.0001")
            field_note = field_note_by_crop.get(product.crop_id)
            if field_note:
                walk_date_label = field_note.walk_date.strftime("%b %d").replace(" 0", " ")
                availability_hint = (
                    f"Last field walk {walk_date_label}: "
                    f"{field_note.get_condition_display().lower()}, "
                    f"{field_note.yield_adjust_pct}% yield"
                )
            elif supply_sale_units > Decimal("0"):
                availability_hint = "Harvest events scheduled this week; no field-walk note yet."
            elif demand_all > Decimal("0"):
                availability_hint = "Demand exists, but no harvest supply is scheduled for this crop/week."
            elif h_units > Decimal("0") and supply_sale_units == Decimal("0"):
                availability_hint = (
                    "Harvest events exist this week for this crop in raw units, but sale units show zero "
                    "— check this product’s harvest_qty_per_sale_unit vs planned harvest units."
                )
            else:
                availability_hint = "No demand or harvest supply scheduled this week."
            hist_cells = []
            for block in historical:
                ev = block["by_product"].get(product.id)
                y = block["calendar_year"]
                ev_prev = historical_prev_by_year[y]["by_product"].get(product.id)
                ev_next = historical_next_by_year[y]["by_product"].get(product.id)
                prev_qty = ev_prev.actual_quantity if ev_prev else None
                next_qty = ev_next.actual_quantity if ev_next else None
                empty_reason = None
                if ev is None:
                    if prev_qty is not None and next_qty is not None:
                        empty_reason = "prior_year_cell_empty_neighbors_both_weeks"
                    elif prev_qty is not None:
                        empty_reason = "prior_year_cell_empty_neighbor_prev_week_only"
                    elif next_qty is not None:
                        empty_reason = "prior_year_cell_empty_neighbor_next_week_only"
                    else:
                        empty_reason = "prior_year_cell_empty_no_neighbor_week_sales"
                empty_cell_title = (
                    _prior_year_empty_cell_title(y, empty_reason)
                    if ev is None and empty_reason
                    else ""
                )
                hist_cells.append(
                    {
                        "calendar_year": block["calendar_year"],
                        "actual_qty": ev.actual_quantity if ev else None,
                        "actual_revenue": ev.actual_revenue if ev else None,
                        "neighbor_prev_qty": prev_qty,
                        "neighbor_next_qty": next_qty,
                        "empty_reason": empty_reason,
                        "empty_cell_title": empty_cell_title,
                    }
                )
            order_rows.append(
                {
                    "product": product,
                    "channel_planned_qty": planned_qty,
                    "demand_all_channels": demand_all,
                    "demand_all_channel_note": demand_all_channel_note,
                    "supply_sale_units": supply_sale_units,
                    "shortage": shortage,
                    "availability_hint": availability_hint,
                    "field_note": field_note,
                    "historical_cells": hist_cells,
                }
            )

        active_order_rows = [
            row
            for row in order_rows
            if row["demand_all_channels"] > Decimal("0") or row["supply_sale_units"] > Decimal("0")
        ]
        shortage_count = sum(1 for row in order_rows if row["shortage"])
        planned_line_count = sum(1 for row in order_rows if row["channel_planned_qty"] > Decimal("0"))
        supply_line_count = sum(1 for row in order_rows if row["supply_sale_units"] > Decimal("0"))
        weekly_demand_visible_count = sum(
            1 for row in order_rows if row["demand_all_channels"] > Decimal("0")
        )

        product_crop_ids = {p.crop_id for p in products}
        harvest_supply_reaches_catalog = bool(harvest_crop_ids & product_crop_ids)
        listed_product_crops_without_harvest_pick = (
            len(product_crop_ids - harvest_crop_ids)
            if weekly_order_products_scope == "crop_plan_week"
            else 0
        )

        supply_diagnostic_hints = harvest_surface_hints(
            week_harvest_event_count=week_harvest_event_count,
            planting_count_excl_dead=planting_count_excl_dead,
            plantings_missing_harvest_events=plantings_missing_harvest,
            weekly_sales_demand_count=weekly_demand_row_count,
            planning_year_id=self.year_obj.id,
            planning_calendar_year=cal_year,
            harvest_supply_reaches_weekly_catalog=harvest_supply_reaches_catalog,
            iso_week=week_num,
            week_monday=week_monday,
            week_sunday=week_sunday,
        )
        supply_diagnostic_hints.extend(
            weekly_order_surface_hints(
                plan_raw_week=plan_raw_week,
                plan_visible_week=plan_visible_week,
                namesake_actuals_on_other_channel=namesake_actuals_on_other_channel,
                channel_name=channel.name,
                has_any_historical=has_any_historical,
                historical_name_fallback=historical_name_fallback,
                historical_product_key_fallback=historical_product_key_fallback,
                positive_week_demand_products=weekly_demand_row_count,
                weekly_demand_visible_count=weekly_demand_visible_count,
                weekly_order_products_scope=weekly_order_products_scope,
                duplicate_channel_name_detected=duplicate_channel_name_detected,
                prior_year_calendar_years=list(prior_year_ints),
                iso_week_has_prior_actuals_any_channel=iso_week_has_prior_actuals_any_channel,
                listed_product_crops_without_harvest_pick=listed_product_crops_without_harvest_pick,
            )
        )

        ctx.update(
            {
                "year": self.year_obj,
                "channel": channel,
                "channels": operator_sales_channels(),
                "week_num": week_num,
                "week_monday": week_monday,
                "week_sunday": week_sunday,
                "order_rows": order_rows,
                "historical_year_columns": historical,
                "prior_week_num": prev_week_num,
                "next_week_num": next_week_num,
                "has_any_historical": has_any_historical,
                "supply_diagnostic_hints": supply_diagnostic_hints,
                "active_order_line_count": len(active_order_rows),
                "shortage_count": shortage_count,
                "planned_line_count": planned_line_count,
                "supply_line_count": supply_line_count,
                "weekly_demand_product_count": weekly_demand_row_count,
                "plan_rows_raw_week": plan_raw_week,
                "plan_rows_visible_week": plan_visible_week,
                "planting_count_excl_dead": planting_count_excl_dead,
                "week_harvest_event_count": week_harvest_event_count,
                "plantings_missing_harvest_events": plantings_missing_harvest,
                "harvest_supply_reaches_catalog": harvest_supply_reaches_catalog,
            }
        )
        ctx.update(self.week_navigation(week_num))
        return ctx


class ProductPriorYearNeighborsView(ReportContextMixin, TemplateView):
    """LIVE-11: one product's imported ACTUALs for prior calendar years at W−1, W, W+1."""

    template_name = "sales/product_prior_year_neighbors.html"

    def dispatch(self, request, *args, **kwargs):
        self.year_obj = get_effective_planning_year(request)
        if not self.year_obj:
            messages.error(request, "No active planning year configured.")
            return redirect("planning:matrix")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        channel = get_object_or_404(SalesChannel, pk=kwargs["channel_id"])
        product = get_object_or_404(CropSalesFormat, pk=kwargs["product_id"])
        week_num = self.resolve_week(kwargs.get("week"))
        cal_year = self.year_obj.year

        in_planning_table = set(
            PlanningYear.objects.filter(year__lt=cal_year).values_list("year", flat=True)
        )
        actual_years = set(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.ACTUAL,
                sale_date__year__lt=cal_year,
            )
            .annotate(sale_year=ExtractYear("sale_date"))
            .values_list("sale_year", flat=True)
        )
        prior_year_ints = sorted(in_planning_table | actual_years, reverse=True)[:6]

        nav = self.week_navigation(week_num)
        prev_w, next_w = nav["prev_week_num"], nav["next_week_num"]

        neighbor_rows = []
        for y in prior_year_ints:
            hp_start, hp_end = self.week_window(y, prev_w)
            hc_start, hc_end = self.week_window(y, week_num)
            hn_start, hn_end = self.week_window(y, next_w)
            b_prev = historical_by_product_for_window(channel, [product], hp_start, hp_end)
            b_cur = historical_by_product_for_window(channel, [product], hc_start, hc_end)
            b_next = historical_by_product_for_window(channel, [product], hn_start, hn_end)

            def pick_qty(block):
                ev = block["by_product"].get(product.id)
                return ev.actual_quantity if ev else None

            neighbor_rows.append(
                {
                    "calendar_year": y,
                    "qty_prev": pick_qty(b_prev),
                    "qty_center": pick_qty(b_cur),
                    "qty_next": pick_qty(b_next),
                    "used_name_fallback": any(
                        (
                            b_prev.get("used_name_fallback"),
                            b_cur.get("used_name_fallback"),
                            b_next.get("used_name_fallback"),
                        )
                    ),
                    "used_product_key_fallback": any(
                        (
                            b_prev.get("used_product_key_fallback"),
                            b_cur.get("used_product_key_fallback"),
                            b_next.get("used_product_key_fallback"),
                        )
                    ),
                }
            )

        ctx.update(
            {
                "year": self.year_obj,
                "channel": channel,
                "product": product,
                "week_num": week_num,
                "neighbor_rows": neighbor_rows,
                "prev_week_num": prev_w,
                "next_week_num": next_w,
            }
        )
        ctx.update(nav)
        return ctx


def _actual_pool_for_prior_calendar_years(
    channel: SalesChannel,
    prior_year_ints: list[int],
) -> tuple[list[SalesEvent], tuple[date, date] | None]:
    """Fetch ACTUAL rows for strict or same-name channels across ISO-week columns in prior years."""
    if not prior_year_ints:
        return [], None
    d0 = min(Week(y, 1).monday() for y in prior_year_ints)
    d1 = max(Week(y, 52).monday() + timedelta(days=6) for y in prior_year_ints)
    pool = list(
        SalesEvent.objects.filter(
            entry_kind=SalesEvent.EntryKind.ACTUAL,
            sale_date__gte=d0,
            sale_date__lte=d1,
        )
        .filter(Q(channel_id=channel.id) | Q(channel__name=channel.name))
        .select_related("product", "product__crop", "channel")
    )
    return pool, (d0, d1)


class ProductYearlyActualsSummaryView(ReportContextMixin, TemplateView):
    """LIVE-11: per prior calendar year, list ISO weeks with non-zero imported ACTUAL qty for one product."""

    template_name = "sales/product_yearly_actuals_summary.html"

    def dispatch(self, request, *args, **kwargs):
        self.year_obj = get_effective_planning_year(request)
        if not self.year_obj:
            messages.error(request, "No active planning year configured.")
            return redirect("planning:matrix")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        channel = get_object_or_404(SalesChannel, pk=kwargs["channel_id"])
        product = get_object_or_404(CropSalesFormat, pk=kwargs["product_id"])
        cal_year = self.year_obj.year

        in_planning_table = set(
            PlanningYear.objects.filter(year__lt=cal_year).values_list("year", flat=True)
        )
        actual_years = set(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.ACTUAL,
                sale_date__year__lt=cal_year,
            )
            .annotate(sale_year=ExtractYear("sale_date"))
            .values_list("sale_year", flat=True)
        )
        prior_year_ints = sorted(in_planning_table | actual_years, reverse=True)[:6]

        pool, bounds = _actual_pool_for_prior_calendar_years(channel, prior_year_ints)

        highlight_raw = (self.request.GET.get("highlight_week") or "").strip()
        highlight_week = None
        if highlight_raw.isdigit():
            highlight_week = self.normalize_week(int(highlight_raw))

        from_week_raw = (self.request.GET.get("from_week") or "").strip()
        if from_week_raw.isdigit():
            weekly_order_back_week = self.normalize_week(int(from_week_raw))
        else:
            weekly_order_back_week = self.resolve_week(None)

        year_sections = []
        for y in prior_year_ints:
            weeks_out = []
            year_total = Decimal("0")
            flags = {"name": False, "remap": False}
            for w in range(1, 53):
                week_start, week_end = self.week_window(y, w)
                block = historical_by_product_for_window_from_pool(
                    pool, channel, [product], week_start, week_end
                )
                if block.get("used_name_fallback"):
                    flags["name"] = True
                if block.get("used_product_key_fallback"):
                    flags["remap"] = True
                ev = block["by_product"].get(product.id)
                qty = ev.actual_quantity if ev else None
                if qty is not None and qty != 0:
                    weeks_out.append(
                        {
                            "iso_week": w,
                            "qty": qty,
                            "highlight": highlight_week == w,
                        }
                    )
                    year_total += qty
            year_sections.append(
                {
                    "calendar_year": y,
                    "weeks": weeks_out,
                    "year_total": year_total,
                    "used_name_fallback": flags["name"],
                    "used_product_key_fallback": flags["remap"],
                }
            )

        ctx.update(
            {
                "year": self.year_obj,
                "channel": channel,
                "product": product,
                "prior_year_ints": prior_year_ints,
                "year_sections": year_sections,
                "pool_bounds": bounds,
                "highlight_week": highlight_week,
                "weekly_order_back_week": weekly_order_back_week,
            }
        )
        return ctx
