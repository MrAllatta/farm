"""reports/views.py"""

import math
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.views.generic import TemplateView
from isoweek import Week

from core.planning_year import resolve_current_planning_year
from planning.models import HarvestEvent, NurseryEvent, Planting
from sales.models import SalesEvent, QuickSalesEntry
from reference.models import Block, SalesChannel, CropSalesFormat
from operations.models import InventoryLedger, PackAllocation, PackBatch, PackBatchComponent
from .mixins import AnalyzeViewMixin, ReportContextMixin
from .services.crop_maps import CropMapOccupancyService


CROP_MAP_PRINT_COLORS = {
    "Greens": "#d9f99d",
    "Vegetables": "#fde68a",
    "Roots": "#fdba74",
    "Brassicas": "#bfdbfe",
    "Alliums": "#ddd6fe",
    "Herbs": "#a7f3d0",
}


class WeeklySchedulePrintView(ReportContextMixin, TemplateView):
    """Weekly Schedule Print"""

    template_name = "reports/weekly_schedule_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        week_num = self.resolve_week(kwargs["week"])
        week_monday, week_sunday = self.week_window(year_obj.year, week_num)

        harvest_events = list(
            HarvestEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .exclude(planting__status__in=self.excluded_statuses)
            .select_related("planting__crop", "planting__block")
            .order_by("planned_date", "planting__block__walk_route_order", "planting__bed_start")
        )
        nursery_events = list(
            NurseryEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .exclude(planting__status__in=self.excluded_statuses)
            .select_related("planting__crop", "planting__block")
            .order_by("planned_date", "event_type", "planting__block__walk_route_order")
        )

        schedule_days = []
        for offset, day_name in enumerate(
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        ):
            day_date = week_monday + timedelta(days=offset)
            is_market_day = day_date.weekday() == 5
            am_tasks = []
            pm_tasks = []
            day_nursery = [event for event in nursery_events if event.planned_date == day_date]
            day_harvest = [event for event in harvest_events if event.planned_date == day_date]

            for event in day_nursery:
                label = (
                    f"{event.get_event_type_display()}: {event.planting.crop.name}"
                    f" ({event.planting.block.name} b{event.planting.bed_start}-{event.planting.bed_end})"
                )
                if event.event_type in {"seed", "pot_up"}:
                    am_tasks.append(label)
                else:
                    pm_tasks.append(label)

            for event in day_harvest:
                pm_tasks.append(
                    f"Harvest {event.planting.crop.name} "
                    f"{event.planned_quantity:.0f} {event.planned_units}"
                )

            if is_market_day:
                am_tasks.insert(0, "Market prep / load-out")
                pm_tasks.append("Market sales and post-market reset")

            schedule_days.append(
                {
                    "name": day_name,
                    "date": day_date,
                    "am_tasks": am_tasks,
                    "pm_tasks": pm_tasks,
                    "is_market_day": is_market_day,
                }
            )

        ctx.update(
            {
                "year": year_obj,
                "view_title": "Weekly Schedule",
                "week_monday": week_monday,
                "week_sunday": week_sunday,
                "schedule_days": schedule_days,
                "nursery_events": nursery_events,
                "transplant_events": [event for event in nursery_events if event.event_type == "transplant"],
                "harvest_events": harvest_events,
                "total_bins": sum(
                    math.ceil(float(event.planned_quantity) / event.planting.crop.units_per_bin)
                    for event in harvest_events
                    if event.planting.crop.units_per_bin and event.planned_quantity
                ),
            }
        )
        ctx.update(self.week_navigation(week_num))
        return ctx


class PackListPrintView(ReportContextMixin, TemplateView):
    """Pack list print — primary source: PackAllocation rows for the week."""

    template_name = "reports/pack_list_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        week_num = self.resolve_week(kwargs["week"])
        week_monday, week_sunday = self.week_window(year_obj.year, week_num)
        channels = list(SalesChannel.objects.all())

        projected_revenue = {}
        for channel in channels:
            planned_total = (
                SalesEvent.objects.filter(
                    planning_year=year_obj,
                    channel=channel,
                    entry_kind=SalesEvent.EntryKind.PLAN,
                    sale_date__gte=week_monday,
                    sale_date__lte=week_sunday,
                ).aggregate(total=Sum("planned_revenue"))["total"]
                or Decimal("0")
            )
            projected_revenue[channel.id] = planned_total

        allocations = (
            PackAllocation.objects.filter(
                pack_date__gte=week_monday,
                pack_date__lte=week_sunday,
            )
            .select_related("product", "product__crop", "channel")
            .order_by("product__crop__name", "product__product_name", "channel_id")
        )

        by_product: dict[int, dict] = {}
        for pa in allocations:
            pid = pa.product_id
            if pid not in by_product:
                p = pa.product
                crop = p.crop
                by_product[pid] = {
                    "product": p,
                    "product_name": p.product_name,
                    "price": p.sale_price,
                    "unit": p.sale_unit,
                    "crop": crop,
                    "channel_qtys": {c.id: Decimal("0") for c in channels},
                    "packed_total": Decimal("0"),
                }
            by_product[pid]["channel_qtys"][pa.channel_id] = by_product[pid]["channel_qtys"].get(
                pa.channel_id, Decimal("0")
            ) + pa.quantity
            by_product[pid]["packed_total"] += pa.quantity

        def _ledger_on_hand(crop_id):
            last = (
                InventoryLedger.objects.filter(crop_id=crop_id)
                .order_by("-event_date", "-created_at", "-id")
                .first()
            )
            return last.running_balance if last else Decimal("0")

        fresh_items = []
        storage_items = []
        for row in sorted(by_product.values(), key=lambda r: (r["crop"].name, r["product_name"])):
            crop = row["crop"]
            item = {
                "product_name": row["product_name"],
                "price": row["price"],
                "harvested": row["packed_total"],
                "unit": row["unit"],
                "channel_qtys": row["channel_qtys"],
                "wholesale_qty": row["packed_total"],
            }
            if crop.fresh_or_storage == "storage":
                item["on_hand"] = _ledger_on_hand(crop.id)
                storage_items.append(item)
            else:
                fresh_items.append(item)

        ctx.update(
            {
                "year": year_obj,
                "pack_date": week_monday + timedelta(days=4),
                "channels": channels,
                "fresh_items": fresh_items,
                "storage_items": storage_items,
                "projected_revenue": projected_revenue,
            }
        )
        ctx.update(self.week_navigation(week_num))
        return ctx


class ExportArchiveView(TemplateView):
    """Export archive"""

    template_name = "reports/harvest_list_print.html"


class ExportCSVView(TemplateView):
    """Export csvs"""

    template_name = "reports/harvest_list_print.html"


class SeedOrderReportView(ReportContextMixin, TemplateView):
    """What-to-order seed report from planned plantings (by crop + variety)."""

    template_name = "reports/seed_order.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("planning", "active", "complete"))
        if not year_obj:
            ctx.update(
                {
                    "year": None,
                    "overplant_pct": 0,
                    "seed_orders": [],
                    "direct_seeded": [],
                    "transplanted": [],
                    "vegetative": [],
                }
            )
            return ctx

        from decimal import Decimal

        from .services.seed_order_report import build_seed_order_rows

        overplant = float(year_obj.overplant_factor)
        plantings = list(
            Planting.objects.filter(planning_year=year_obj)
            .exclude(status="skipped")
            .select_related("crop", "crop_season", "block", "variety_obj")
        )
        seed_orders = build_seed_order_rows(plantings, overplant)

        ctx.update(
            {
                "year": year_obj,
                "overplant_pct": int((year_obj.overplant_factor - Decimal(1)) * Decimal(100)),
                "seed_orders": seed_orders,
                "direct_seeded": [s for s in seed_orders if s["method"] == "direct_seed"],
                "transplanted": [s for s in seed_orders if s["method"] == "transplant"],
                "vegetative": [s for s in seed_orders if s["method"] == "vegetative"],
            }
        )
        return ctx


class NurserySchedulePrintView(ReportContextMixin, TemplateView):
    """Generate print-ready nursery schedule."""

    template_name = "reports/nursery_schedule_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("planning", "active", "complete"))
        nursery_events = list(
            NurseryEvent.objects.filter(planting__planning_year=year_obj)
            .exclude(planting__status__in=self.excluded_statuses)
            .select_related("planting__crop", "planting__block")
            .order_by("planned_date", "event_type", "planting__crop__name")
        )

        weeks = {}
        for event in nursery_events:
            week_num = event.planned_date.isocalendar()[1]
            bucket = weeks.setdefault(
                week_num,
                {
                    "week_num": week_num,
                    "monday": Week(year_obj.year, week_num).monday(),
                    "events": [],
                    "bench_trays": 0,
                },
            )
            bucket["events"].append(event)
            bucket["bench_trays"] += event.planned_tray_count or 0

        all_weeks = [weeks[key] for key in sorted(weeks)]
        peak_week = max(
            ({"week": item["week_num"], "trays": item["bench_trays"]} for item in all_weeks),
            key=lambda item: item["trays"],
            default={"week": None, "trays": 0},
        )

        ctx.update(
            {
                "year": year_obj,
                "today": date.today(),
                "greenhouse_capacity": 0,
                "bench_by_week": [
                    {
                        "week": item["week_num"],
                        "trays": item["bench_trays"],
                        "over_capacity": False,
                    }
                    for item in all_weeks
                ],
                "all_nursery_weeks": all_weeks,
                "total_seed_events": sum(1 for event in nursery_events if event.event_type == "seed"),
                "total_potup_events": sum(1 for event in nursery_events if event.event_type == "pot_up"),
                "total_transplant_events": sum(
                    1 for event in nursery_events if event.event_type == "transplant"
                ),
                "peak_week": peak_week,
            }
        )
        return ctx


class HarvestListPrintView(ReportContextMixin, TemplateView):
    """Generates a print-ready harvest list for a given week."""

    template_name = "reports/harvest_list_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        week_num = self.resolve_week(kwargs["week"])
        week_monday, week_sunday = self.week_window(year_obj.year, week_num)

        events = (
            HarvestEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .exclude(planting__status__in=self.excluded_statuses)
            .select_related("planting__crop", "planting__block")
            .order_by(
                "planting__block__walk_route_order",
                "planting__block__name",
                "planting__bed_start",
            )
        )

        items = []
        bin_totals = {}  # bin_type → count
        tools_needed = set()

        for he in events:
            crop = he.planting.crop
            bins_needed = None
            if crop.units_per_bin and he.planned_quantity:
                bins_needed = math.ceil(float(he.planned_quantity) / crop.units_per_bin)
                bin_type = crop.harvest_bin or "unknown"
                bin_totals[bin_type] = bin_totals.get(bin_type, 0) + bins_needed

            if crop.harvest_tools:
                tools_needed.add(crop.harvest_tools)

            items.append(
                {
                    "crop": crop.name,
                    "block": he.planting.block.name,
                    "beds": f"{he.planting.bed_start}-{he.planting.bed_end}",
                    "target_qty": he.planned_quantity,
                    "units": he.planned_units,
                    "bins_needed": bins_needed,
                    "bin_type": crop.harvest_bin,
                    "harvest_tools": crop.harvest_tools,
                }
            )

        # Calculate harvest day (typically Thursday for Sat market)
        # This could be configurable
        harvest_day = week_monday + timedelta(days=3)  # Thursday

        ctx.update(
            {
                "view_title": "Harvest List",
                "year": year_obj,
                "harvest_day": harvest_day,
                "items": items,
                "bin_totals": sorted(bin_totals.items()),
                "total_bins": sum(bin_totals.values()),
                "tools_needed": sorted(tools_needed),
                "total_items": len(items),
            }
        )
        ctx.update(self.week_navigation(week_num))
        return ctx


class RevenueProjectionView(TemplateView):
    """Project weekly revenue from planned plantings × sales formats."""

    template_name = "reports/revenue_projection.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        year = year_obj.year

        channels = SalesChannel.objects.all()

        # Build weekly harvest availability: week → crop → quantity
        harvest_events = (
            HarvestEvent.objects.filter(
                planting__planning_year=year_obj,
            )
            .exclude(planting__status__in=["skipped", "failed", "revised"])
            .select_related("planting__crop", "planting__crop_season")
        )

        # Aggregate expected harvest by week and crop
        weekly_supply = {}  # week_num → {crop_id: total_qty}
        for he in harvest_events:
            wk = he.planned_date.isocalendar()[1]
            crop_id = he.planting.crop_id

            if wk not in weekly_supply:
                weekly_supply[wk] = {}

            qty = float(he.actual_quantity or he.planned_quantity or 0)
            weekly_supply[wk][crop_id] = weekly_supply[wk].get(crop_id, 0) + qty

        # Get all active sales formats with their prices
        formats = CropSalesFormat.objects.filter(is_active=True).select_related("crop")

        # Build format lookup: crop_id → best format (highest price)
        crop_formats = {}
        for f in formats:
            if f.crop_id not in crop_formats:
                crop_formats[f.crop_id] = f
            elif f.sale_price > crop_formats[f.crop_id].sale_price:
                crop_formats[f.crop_id] = f

        # Project revenue per week
        weekly_projections = []
        annual_projected = Decimal("0")
        annual_target = sum(ch.annual_target for ch in channels)

        for wk in range(1, 53):
            supply = weekly_supply.get(wk, {})

            week_revenue = Decimal("0")
            week_products = []

            for crop_id, qty in supply.items():
                fmt = crop_formats.get(crop_id)
                if fmt:
                    sale_units = Decimal(str(qty)) / fmt.harvest_qty_per_sale_unit
                    revenue = sale_units * fmt.sale_price
                    week_revenue += revenue
                    week_products.append(
                        {
                            "crop_name": fmt.crop.name,
                            "quantity": qty,
                            "harvest_unit": fmt.crop.harvest_unit,
                            "revenue": revenue,
                        }
                    )

            # Compare to channel targets for this week
            week_target = Decimal("0")
            for ch in channels:
                if ch.start_week <= wk <= ch.end_week:
                    week_target += ch.weekly_target

            gap = week_revenue - week_target
            annual_projected += week_revenue

            monday = Week(year, wk).monday()

            weekly_projections.append(
                {
                    "week": wk,
                    "date": monday,
                    "projected_revenue": week_revenue,
                    "target": week_target,
                    "gap": gap,
                    "gap_pct": (gap / week_target * 100) if week_target else 0,
                    "num_products": len(week_products),
                    "products": sorted(week_products, key=lambda x: x["revenue"], reverse=True)[:5],
                }
            )

        # Identify problem periods
        gap_weeks = [w for w in weekly_projections if w["gap"] < 0]
        surplus_weeks = [w for w in weekly_projections if w["gap"] > 0]

        ctx.update(
            {
                "year": year_obj,
                "channels": channels,
                "weekly": weekly_projections,
                "annual_projected": annual_projected,
                "annual_target": annual_target,
                "annual_gap": annual_projected - annual_target,
                "gap_weeks": len(gap_weeks),
                "surplus_weeks": len(surplus_weeks),
                "worst_gap_week": min(gap_weeks, key=lambda w: w["gap"]) if gap_weeks else None,
                "best_surplus_week": (
                    max(surplus_weeks, key=lambda w: w["gap"]) if surplus_weeks else None
                ),
            }
        )
        return ctx


class CropPerformanceView(AnalyzeViewMixin, TemplateView):
    """Per-crop analysis: yield, revenue, $/bedfoot."""

    analyze_page = "crop_performance"
    page_title = "Crop Performance"
    template_name = "reports/crop_performance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year()
        if not year_obj:
            ctx.update(
                self.build_analyze_context(
                    None,
                    page_subtitle="No active or completed planning year is available yet.",
                    empty_message="Create or activate a planning year to analyze crop performance.",
                )
            )
            ctx.update(
                {
                    "crops": [],
                    "total_revenue": Decimal("0"),
                    "total_bedfeet": 0,
                    "avg_revenue_per_bf": Decimal("0"),
                    "num_crops": 0,
                }
            )
            return ctx

        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
            )
            .exclude(status="skipped")
            .select_related("crop", "crop_season", "block")
            .prefetch_related(
                "harvest_events",
            )
        )

        # Aggregate by crop
        crop_data = {}

        for p in plantings:
            crop_name = p.crop.name
            if crop_name not in crop_data:
                crop_data[crop_name] = {
                    "crop": p.crop,
                    "crop_season": p.crop_season,
                    "plantings": [],
                    "total_planned_bedfeet": 0,
                    "total_actual_bedfeet": 0,
                    "total_planned_yield": Decimal("0"),
                    "total_actual_yield": Decimal("0"),
                    "harvest_unit": p.crop.harvest_unit,
                    "weeks_occupied": 0,
                }

            d = crop_data[crop_name]
            d["plantings"].append(p)
            d["total_planned_bedfeet"] += p.planned_bedfeet
            d["total_actual_bedfeet"] += p.actual_bedfeet or p.planned_bedfeet
            d["total_planned_yield"] += p.planned_total_yield or Decimal("0")

            # Sum actual harvest
            actual_sum = p.harvest_events.filter(actual_quantity__isnull=False).aggregate(
                total=Sum("actual_quantity")
            )["total"]

            if actual_sum:
                d["total_actual_yield"] += actual_sum

            # Calculate weeks occupied
            if p.planned_plant_date and p.planned_last_harvest_date:
                weeks = (p.planned_last_harvest_date - p.planned_plant_date).days / 7
                d["weeks_occupied"] = max(d["weeks_occupied"], weeks)

        # Calculate revenue per crop from sales events
        from sales.models import SalesEvent

        for crop_name, d in crop_data.items():
            # Find sales formats for this crop
            formats = CropSalesFormat.objects.filter(crop=d["crop"])

            total_revenue = SalesEvent.objects.filter(
                product__crop=d["crop"],
                sale_date__year=year_obj.year,
                actual_revenue__isnull=False,
            ).aggregate(total=Sum("actual_revenue"))["total"] or Decimal("0")

            d["total_revenue"] = total_revenue

            bf = d["total_actual_bedfeet"] or d["total_planned_bedfeet"]
            d["revenue_per_bedfoot"] = total_revenue / bf if bf else Decimal("0")

            d["planned_yield_per_bf"] = (
                d["total_planned_yield"] / d["total_planned_bedfeet"]
                if d["total_planned_bedfeet"]
                else Decimal("0")
            )
            d["actual_yield_per_bf"] = (
                d["total_actual_yield"] / bf if bf and d["total_actual_yield"] else None
            )

            d["yield_variance_pct"] = None
            if d["actual_yield_per_bf"] and d["planned_yield_per_bf"]:
                d["yield_variance_pct"] = (
                    (d["actual_yield_per_bf"] - d["planned_yield_per_bf"])
                    / d["planned_yield_per_bf"]
                    * 100
                )

            # $/bedfoot/week (penalizes crops that occupy space longer)
            if d["weeks_occupied"] and d["revenue_per_bedfoot"]:
                d["revenue_per_bf_per_week"] = d["revenue_per_bedfoot"] / Decimal(
                    str(d["weeks_occupied"])
                )
            else:
                d["revenue_per_bf_per_week"] = Decimal("0")

        # Sort by $/bedfoot descending
        performance = sorted(
            crop_data.values(),
            key=lambda d: d["revenue_per_bedfoot"],
            reverse=True,
        )

        # Summary stats
        total_revenue = sum(d["total_revenue"] for d in performance)
        total_bedfeet = sum(
            d["total_actual_bedfeet"] or d["total_planned_bedfeet"] for d in performance
        )

        ctx.update(
            {
                "crops": performance,
                "total_revenue": total_revenue,
                "total_bedfeet": total_bedfeet,
                "avg_revenue_per_bf": (total_revenue / total_bedfeet if total_bedfeet else 0),
                "num_crops": len(performance),
            }
        )
        ctx.update(
            self.build_analyze_context(
                year_obj,
                page_subtitle="Yield and revenue per crop across completed and in-season plantings.",
                empty_message=(
                    "No crop performance data is available yet for this planning year."
                    if not performance
                    else ""
                ),
            )
        )
        return ctx


class ChannelPerformanceView(AnalyzeViewMixin, TemplateView):
    """Revenue by channel — weekly, monthly, annual vs target."""

    analyze_page = "channel_performance"
    page_title = "Channel Performance"
    template_name = "reports/channel_performance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year()
        if not year_obj:
            ctx.update(
                self.build_analyze_context(
                    None,
                    page_subtitle="No active or completed planning year is available yet.",
                    empty_message="Create or activate a planning year to analyze channel performance.",
                )
            )
            ctx.update(
                {
                    "channels": [],
                    "total_ytd": Decimal("0"),
                    "total_target_ytd": Decimal("0"),
                    "total_annual_target": Decimal("0"),
                    "total_ytd_gap": Decimal("0"),
                }
            )
            return ctx
        year = year_obj.year

        channels = SalesChannel.objects.all()

        channel_data = []

        for channel in channels:
            # Get all weeks this channel is active
            active_weeks = list(range(channel.start_week, channel.end_week + 1))

            # Collect revenue per week (from detailed or quick entries)
            weekly_revenue = {}

            # From detailed sales events
            detailed = (
                SalesEvent.objects.filter(
                    channel=channel,
                    sale_date__year=year,
                    actual_revenue__isnull=False,
                )
                .values("sale_date")
                .annotate(day_total=Sum("actual_revenue"))
            )

            for row in detailed:
                wk = row["sale_date"].isocalendar()[1]
                weekly_revenue[wk] = weekly_revenue.get(wk, Decimal("0")) + row["day_total"]

            # From quick entries (fills gaps where detailed not used)
            quick = QuickSalesEntry.objects.filter(
                channel=channel,
                sale_date__year=year,
            )

            for qe in quick:
                wk = qe.sale_date.isocalendar()[1]
                # Only use quick entry if no detailed entries for this week
                if wk not in weekly_revenue:
                    weekly_revenue[wk] = qe.total_revenue

            # Build week-by-week table
            weeks_table = []
            ytd_revenue = Decimal("0")
            ytd_target = Decimal("0")

            for wk in active_weeks:
                revenue = weekly_revenue.get(wk, None)
                target = channel.weekly_target

                ytd_target += target
                if revenue is not None:
                    ytd_revenue += revenue

                gap = (revenue - target) if revenue is not None else None

                weeks_table.append(
                    {
                        "week": wk,
                        "date": Week(year, wk).monday(),
                        "revenue": revenue,
                        "target": target,
                        "gap": gap,
                        "gap_pct": (gap / target * 100) if gap is not None and target else None,
                        "has_data": revenue is not None,
                    }
                )

            # Monthly aggregates
            monthly = {}
            for row in weeks_table:
                month = row["date"].month
                if month not in monthly:
                    monthly[month] = {
                        "month": month,
                        "month_name": row["date"].strftime("%B"),
                        "revenue": Decimal("0"),
                        "target": Decimal("0"),
                        "weeks": 0,
                        "weeks_with_data": 0,
                    }
                monthly[month]["target"] += row["target"]
                monthly[month]["weeks"] += 1
                if row["revenue"] is not None:
                    monthly[month]["revenue"] += row["revenue"]
                    monthly[month]["weeks_with_data"] += 1

            # Sell-through analysis (only if detailed sales exist)
            sellthrough_data = None
            if SalesEvent.objects.filter(
                channel=channel,
                sale_date__year=year,
                brought_quantity__isnull=False,
            ).exists():
                st = SalesEvent.objects.filter(
                    channel=channel,
                    sale_date__year=year,
                    brought_quantity__isnull=False,
                    brought_quantity__gt=0,
                ).aggregate(
                    total_brought=Sum("brought_quantity"),
                    total_sold=Sum("actual_quantity"),
                )

                if st["total_brought"]:
                    sellthrough_data = {
                        "total_brought": st["total_brought"],
                        "total_sold": st["total_sold"],
                        "pct": (st["total_sold"] / st["total_brought"] * 100),
                    }

            # Top products by revenue
            top_products = (
                SalesEvent.objects.filter(
                    channel=channel,
                    sale_date__year=year,
                    actual_revenue__isnull=False,
                )
                .values(
                    "product__product_name",
                    "product__crop__name",
                )
                .annotate(
                    total_revenue=Sum("actual_revenue"),
                    total_qty=Sum("actual_quantity"),
                )
                .order_by("-total_revenue")[:10]
            )

            # Pacing analysis — are we on track?
            today = date.today()
            weeks_elapsed = sum(1 for w in weeks_table if w["date"] <= today and w["has_data"])
            weeks_remaining = sum(1 for w in weeks_table if w["date"] > today)

            on_pace = None
            if weeks_elapsed > 0:
                avg_actual = ytd_revenue / weeks_elapsed
                projected_annual = ytd_revenue + avg_actual * weeks_remaining
                on_pace = projected_annual >= channel.annual_target

                pacing_data = {
                    "avg_weekly": avg_actual,
                    "projected_annual": projected_annual,
                    "annual_target": channel.annual_target,
                    "on_pace": on_pace,
                    "gap_to_target": projected_annual - channel.annual_target,
                }
            else:
                pacing_data = None

            channel_data.append(
                {
                    "channel": channel,
                    "weeks_table": weeks_table,
                    "monthly": sorted(monthly.values(), key=lambda m: m["month"]),
                    "ytd_revenue": ytd_revenue,
                    "ytd_target": ytd_target,
                    "ytd_gap": ytd_revenue - ytd_target,
                    "annual_target": channel.annual_target,
                    "weeks_with_data": sum(1 for w in weeks_table if w["has_data"]),
                    "total_active_weeks": len(active_weeks),
                    "sellthrough": sellthrough_data,
                    "top_products": list(top_products),
                    "pacing": pacing_data,
                }
            )

        # Grand totals across all channels
        total_ytd = sum(cd["ytd_revenue"] for cd in channel_data)
        total_target_ytd = sum(cd["ytd_target"] for cd in channel_data)
        total_annual_target = sum(cd["annual_target"] for cd in channel_data)

        ctx.update(
            {
                "channels": channel_data,
                "total_ytd": total_ytd,
                "total_target_ytd": total_target_ytd,
                "total_annual_target": total_annual_target,
                "total_ytd_gap": total_ytd - total_target_ytd,
            }
        )
        ctx.update(
            self.build_analyze_context(
                year_obj,
                page_subtitle="Weekly pacing, target attainment, and top products by channel.",
                empty_message=(
                    "No sales channels or recorded sales data are available for this planning year."
                    if not channel_data
                    else ""
                ),
            )
        )
        return ctx


class SeasonSummaryView(AnalyzeViewMixin, TemplateView):
    """End-of-season overview — the post-season analysis dashboard."""

    analyze_page = "season_summary"
    page_title = "Season Summary"
    template_name = "reports/season_summary.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year()
        if not year_obj:
            ctx.update(
                self.build_analyze_context(
                    None,
                    page_subtitle="No active or completed planning year is available yet.",
                    empty_message="Create or activate a planning year to view a season summary.",
                )
            )
            ctx.update(
                {
                    "total_plantings": 0,
                    "completed_plantings": 0,
                    "failed_plantings": 0,
                    "skipped_plantings": 0,
                    "failure_rate": 0,
                    "total_planned_bf": 0,
                    "total_actual_bf": 0,
                    "planned_yield_total": Decimal("0"),
                    "actual_yield_total": Decimal("0"),
                    "yield_attainment": None,
                    "total_revenue": Decimal("0"),
                    "annual_target": Decimal("0"),
                    "revenue_attainment": None,
                    "revenue_gap": Decimal("0"),
                    "revenue_per_bf": Decimal("0"),
                    "total_harvest_hours": Decimal("0"),
                    "revenue_per_harvest_hour": None,
                    "unique_crops": 0,
                    "crop_types": [],
                    "botanical_families": [],
                    "top_10_crops": [],
                    "bottom_10_crops": [],
                    "rotation_updates": 0,
                }
            )
            return ctx
        year = year_obj.year

        # All plantings
        all_plantings = Planting.objects.filter(
            planning_year=year_obj,
        ).select_related("crop", "crop_season", "block")

        planned = all_plantings.exclude(status="skipped")
        completed = all_plantings.filter(status__in=["complete", "harvesting"])
        failed = all_plantings.filter(status="failed")
        skipped = all_plantings.filter(status="skipped")

        # Total bedfeet
        total_planned_bf = sum(p.planned_bedfeet for p in planned)
        total_actual_bf = sum(p.actual_bedfeet or p.planned_bedfeet for p in completed)

        # Yield summary
        harvest_totals = HarvestEvent.objects.filter(
            planting__planning_year=year_obj,
        ).aggregate(
            planned_yield=Sum("planned_quantity"),
            actual_yield=Sum("actual_quantity"),
        )

        planned_yield_total = harvest_totals["planned_yield"] or Decimal("0")
        actual_yield_total = harvest_totals["actual_yield"] or Decimal("0")

        # Revenue summary — all channels
        detailed_revenue = SalesEvent.objects.filter(
            sale_date__year=year,
            actual_revenue__isnull=False,
        ).aggregate(total=Sum("actual_revenue"))["total"] or Decimal("0")

        quick_revenue = QuickSalesEntry.objects.filter(
            sale_date__year=year,
        ).aggregate(
            total=Sum("total_cash") + Sum("total_card")
        )["total"] or Decimal("0")

        # Avoid double-counting: detailed sales take precedence by week
        # Get weeks covered by detailed sales
        detailed_weeks = set(
            SalesEvent.objects.filter(
                sale_date__year=year,
                actual_revenue__isnull=False,
            )
            .values_list("sale_date", flat=True)
            .distinct()
        )
        detailed_week_nums = {d.isocalendar()[1] for d in detailed_weeks}

        # Quick sales for weeks NOT covered by detailed
        quick_only = QuickSalesEntry.objects.filter(
            sale_date__year=year,
        ).exclude(
            sale_date__in=detailed_weeks
        ).aggregate(total=Sum("total_cash") + Sum("total_card"))["total"] or Decimal("0")

        total_revenue = detailed_revenue + quick_only

        annual_target = sum(ch.annual_target for ch in SalesChannel.objects.all())

        # Mix reconciliation: sold mix qty versus packed qty and component drawdown.
        mix_batches = PackBatch.objects.filter(pack_date__year=year).select_related("product")
        mix_sales = SalesEvent.objects.filter(
            sale_date__year=year,
            pack_batch__isnull=False,
            actual_quantity__isnull=False,
        )
        mix_packed_qty = (
            mix_batches.aggregate(total=Sum("packed_quantity"))["total"] or Decimal("0")
        )
        mix_sold_qty = (
            mix_sales.aggregate(total=Sum("actual_quantity"))["total"] or Decimal("0")
        )
        mix_component_drawdown = (
            PackBatchComponent.objects.filter(
                pack_batch__pack_date__year=year,
                source_crop__isnull=False,
            ).aggregate(total=Sum("consumed_quantity"))["total"]
            or Decimal("0")
        )

        # Crops grown
        crop_types = set(p.crop.crop_type for p in planned if p.crop.crop_type)
        unique_crops = set(p.crop.name for p in planned)
        botanical_families = set(
            p.crop.botanical_family for p in planned if p.crop.botanical_family
        )

        # Harvest labor
        labor_totals = HarvestEvent.objects.filter(
            planting__planning_year=year_obj,
            actual_hours__isnull=False,
        ).aggregate(
            total_hours=Sum("actual_hours"),
        )
        total_harvest_hours = labor_totals["total_hours"] or Decimal("0")

        revenue_per_harvest_hour = (
            total_revenue / total_harvest_hours if total_harvest_hours else None
        )

        # Crop performance — top and bottom performers by $/bf
        crop_performance = {}

        for p in completed:
            crop_name = p.crop.name
            if crop_name not in crop_performance:
                crop_performance[crop_name] = {
                    "crop": p.crop,
                    "total_bf": 0,
                    "total_revenue": Decimal("0"),
                }

            bf = p.actual_bedfeet or p.planned_bedfeet
            crop_performance[crop_name]["total_bf"] += bf

            # Estimate revenue from harvest × price
            harvest = p.harvest_events.filter(actual_quantity__isnull=False).aggregate(
                total=Sum("actual_quantity")
            )["total"]

            if harvest:
                fmt = (
                    CropSalesFormat.objects.filter(crop=p.crop, is_active=True)
                    .order_by("-sale_price")
                    .first()
                )

                if fmt:
                    revenue = harvest / fmt.harvest_qty_per_sale_unit * fmt.sale_price
                    crop_performance[crop_name]["total_revenue"] += revenue

        for name, data in crop_performance.items():
            data["revenue_per_bf"] = (
                data["total_revenue"] / data["total_bf"] if data["total_bf"] else Decimal("0")
            )

        sorted_by_revenue = sorted(
            crop_performance.values(),
            key=lambda d: d["revenue_per_bf"],
            reverse=True,
        )

        top_10 = sorted_by_revenue[:10]
        bottom_10 = [c for c in sorted_by_revenue[-10:] if c["total_revenue"] > 0]

        # Rotation summary — update rotation history
        # (Could be automated at season completion)
        rotation_updates = {}
        for p in completed:
            family = p.crop.botanical_family
            if family and p.block_id:
                key = (p.block_id, family)
                rotation_updates[key] = True

        ctx.update(
            {
                # Plantings overview
                "total_plantings": all_plantings.count(),
                "completed_plantings": completed.count(),
                "failed_plantings": failed.count(),
                "skipped_plantings": skipped.count(),
                "failure_rate": (failed.count() / planned.count() * 100 if planned.count() else 0),
                # Space
                "total_planned_bf": total_planned_bf,
                "total_actual_bf": total_actual_bf,
                # Yield
                "planned_yield_total": planned_yield_total,
                "actual_yield_total": actual_yield_total,
                "yield_attainment": (
                    actual_yield_total / planned_yield_total * 100 if planned_yield_total else None
                ),
                # Revenue
                "total_revenue": total_revenue,
                "annual_target": annual_target,
                "revenue_attainment": (
                    total_revenue / annual_target * 100 if annual_target else None
                ),
                "revenue_gap": total_revenue - annual_target,
                "revenue_per_bf": (
                    total_revenue / total_actual_bf if total_actual_bf else Decimal("0")
                ),
                # Labor
                "total_harvest_hours": total_harvest_hours,
                "revenue_per_harvest_hour": revenue_per_harvest_hour,
                # Mix reconciliation
                "mix_batches_count": mix_batches.count(),
                "mix_packed_qty": mix_packed_qty,
                "mix_sold_qty": mix_sold_qty,
                "mix_unallocated_qty": mix_packed_qty - mix_sold_qty,
                "mix_component_drawdown": mix_component_drawdown,
                # Diversity
                "unique_crops": len(unique_crops),
                "crop_types": sorted(crop_types),
                "botanical_families": sorted(botanical_families),
                # Performers
                "top_10_crops": top_10,
                "bottom_10_crops": list(reversed(bottom_10)),
                # For rotation update UI
                "rotation_updates": len(rotation_updates),
            }
        )
        ctx.update(
            self.build_analyze_context(
                year_obj,
                page_subtitle="Season-wide rollup of plantings, yield, revenue, and labor.",
                empty_message=(
                    "No planting or sales data is available yet for this planning year."
                    if not all_plantings.exists()
                    else ""
                ),
            )
        )
        return ctx


class CropMapView(ReportContextMixin, TemplateView):
    """Spatial farm view showing what's planted where."""

    template_name = "reports/crop_map.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        week_num = self.resolve_week(kwargs.get("week"))
        week_date = self.week_window(year_obj.year, week_num)[0]
        service = CropMapOccupancyService(year_obj)
        block_rows = service.get_high_level_block_map(week_num=week_num)
        field_maps = [bm for bm in block_rows if bm["block"].block_type == "field"]
        tunnel_maps = [bm for bm in block_rows if bm["block"].block_type == "high_tunnel"]
        greenhouse_maps = [bm for bm in block_rows if bm["block"].block_type == "greenhouse"]
        total_bf = sum(row["block"].total_bedfeet for row in block_rows)
        used_bf = sum(
            row["block"].total_bedfeet * (row["utilization_pct"] / 100)
            for row in block_rows
        )

        ctx.update(
            {
                "year": year_obj,
                "week_date": week_date,
                "field_maps": field_maps,
                "tunnel_maps": tunnel_maps,
                "greenhouse_maps": greenhouse_maps,
                "total_bf": total_bf,
                "overall_utilization": (used_bf / total_bf * 100) if total_bf else 0,
                "view_links": [
                    ("High-Level Block Map", "reports:crop_map"),
                    ("Week By Bed Grid", "reports:crop_map_week_by_bed"),
                    ("Week By Block Grid", "reports:crop_map_week_by_block"),
                    ("Successions By Block", "reports:crop_map_successions"),
                ],
            }
        )
        ctx.update(self.week_navigation(week_num))
        return ctx


class BlockUtilizationView(AnalyzeViewMixin, TemplateView):
    """Per-block analysis: weeks in use, revenue, $/bf."""

    analyze_page = "block_utilization"
    page_title = "Block Utilization"
    template_name = "reports/block_utilization.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year()
        if not year_obj:
            ctx.update(
                self.build_analyze_context(
                    None,
                    page_subtitle="No active or completed planning year is available yet.",
                    empty_message="Create or activate a planning year to analyze block utilization.",
                )
            )
            ctx.update(
                {
                    "blocks": [],
                    "total_revenue": Decimal("0"),
                    "total_bf": 0,
                    "avg_utilization": 0,
                    "avg_revenue_per_bf": Decimal("0"),
                }
            )
            return ctx

        blocks = Block.objects.all().order_by("walk_route_order", "name")

        block_data = []

        for block in blocks:
            plantings = (
                Planting.objects.filter(
                    planning_year=year_obj,
                    block=block,
                )
                .exclude(status__in=self.excluded_statuses)
                .select_related("crop")
            )

            if not plantings.exists():
                block_data.append(
                    {
                        "block": block,
                        "num_plantings": 0,
                        "weeks_used": 0,
                        "weeks_fallow": 52,
                        "utilization_pct": 0,
                        "total_revenue": Decimal("0"),
                        "revenue_per_bf": Decimal("0"),
                        "revenue_per_bf_per_week": Decimal("0"),
                        "crops_grown": [],
                        "families": set(),
                    }
                )
                continue

            # Calculate weeks in use
            # A bed-week is occupied if any planting covers that bed in that week
            occupied_weeks = set()
            crops_grown = []
            families = set()

            for p in plantings:
                plant_wk = p.planned_plant_date.isocalendar()[1]
                end_wk = p.planned_last_harvest_date.isocalendar()[1]

                # Handle year boundary (rare for field crops but possible)
                if end_wk >= plant_wk:
                    for wk in range(plant_wk, end_wk + 1):
                        occupied_weeks.add(wk)
                else:
                    for wk in range(plant_wk, 53):
                        occupied_weeks.add(wk)
                    for wk in range(1, end_wk + 1):
                        occupied_weeks.add(wk)

                crops_grown.append(p.crop.name)
                if p.crop.botanical_family:
                    families.add(p.crop.botanical_family)

            weeks_used = len(occupied_weeks)
            weeks_fallow = 52 - weeks_used

            # Revenue from plantings in this block
            from sales.models import SalesEvent

            total_revenue = Decimal("0")

            for p in plantings:
                # Sum harvest actuals as proxy for revenue
                # Proper revenue requires tracing through sales events
                harvest_total = p.harvest_events.filter(
                    actual_quantity__isnull=False,
                ).aggregate(
                    total=Sum("actual_quantity")
                )["total"]

                if harvest_total:
                    # Find best sales format for price
                    fmt = (
                        CropSalesFormat.objects.filter(crop=p.crop, is_active=True)
                        .order_by("-sale_price")
                        .first()
                    )

                    if fmt:
                        units = harvest_total / fmt.harvest_qty_per_sale_unit
                        total_revenue += units * fmt.sale_price

            bf = block.total_bedfeet

            block_data.append(
                {
                    "block": block,
                    "num_plantings": plantings.count(),
                    "weeks_used": weeks_used,
                    "weeks_fallow": weeks_fallow,
                    "utilization_pct": weeks_used / 52 * 100,
                    "total_revenue": total_revenue,
                    "revenue_per_bf": total_revenue / bf if bf else Decimal("0"),
                    "revenue_per_bf_per_week": (total_revenue / bf / 52 if bf else Decimal("0")),
                    "crops_grown": sorted(set(crops_grown)),
                    "families": families,
                }
            )

        # Sort by revenue per bedfoot per week (descending)
        block_data.sort(key=lambda b: b["revenue_per_bf_per_week"], reverse=True)

        # Totals
        total_revenue = sum(b["total_revenue"] for b in block_data)
        total_bf = sum(b["block"].total_bedfeet for b in block_data)
        avg_utilization = (
            sum(b["utilization_pct"] for b in block_data) / len(block_data) if block_data else 0
        )

        ctx.update(
            {
                "blocks": block_data,
                "total_revenue": total_revenue,
                "total_bf": total_bf,
                "avg_utilization": avg_utilization,
                "avg_revenue_per_bf": (total_revenue / total_bf if total_bf else 0),
            }
        )
        ctx.update(
            self.build_analyze_context(
                year_obj,
                page_subtitle="Compare block occupancy, crop mix, and proxy revenue by space used.",
                empty_message=(
                    "No blocks or plantings are available yet for this planning year."
                    if not block_data
                    else ""
                ),
            )
        )
        return ctx


class PlanVsActualView(AnalyzeViewMixin, TemplateView):
    """Per-planting comparison of plan to reality."""

    analyze_page = "plan_vs_actual"
    page_title = "Plan vs Actual"
    template_name = "reports/plan_vs_actual.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year()
        if not year_obj:
            ctx.update(
                self.build_analyze_context(
                    None,
                    page_subtitle="No active or completed planning year is available yet.",
                    empty_message="Create or activate a planning year to compare planned and actual results.",
                )
            )
            ctx.update(
                {
                    "rows": [],
                    "total_plantings": 0,
                    "with_actuals": 0,
                    "overperformers": [],
                    "underperformers": [],
                }
            )
            return ctx

        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
            )
            .exclude(status__in=self.excluded_statuses)
            .select_related("crop", "crop_season", "block")
            .prefetch_related("harvest_events")
            .order_by("crop__crop_type", "crop__name", "block__name")
        )

        rows = []

        for p in plantings:
            actual_harvests = p.harvest_events.filter(actual_quantity__isnull=False)
            actual_yield = actual_harvests.aggregate(total=Sum("actual_quantity"))["total"]

            actual_hours = actual_harvests.aggregate(total=Sum("actual_hours"))["total"]

            planned_yield = p.planned_total_yield or Decimal("0")
            bf = p.actual_bedfeet or p.planned_bedfeet

            yield_variance = None
            yield_variance_pct = None
            if actual_yield is not None and planned_yield:
                yield_variance = actual_yield - planned_yield
                yield_variance_pct = yield_variance / planned_yield * 100

            actual_yield_per_bf = None
            if actual_yield and bf:
                actual_yield_per_bf = actual_yield / bf

            planned_yield_per_bf = None
            if planned_yield and bf:
                planned_yield_per_bf = planned_yield / bf

            # Timing variance
            plant_variance_days = None
            if p.actual_plant_date and p.planned_plant_date:
                plant_variance_days = (p.actual_plant_date - p.planned_plant_date).days

            harvest_variance_days = None
            if p.actual_first_harvest_date and p.planned_first_harvest_date:
                harvest_variance_days = (
                    p.actual_first_harvest_date - p.planned_first_harvest_date
                ).days

            rows.append(
                {
                    "planting": p,
                    "crop_name": p.crop.name,
                    "crop_type": p.crop.crop_type,
                    "block": p.block.name,
                    "beds": f"{p.bed_start}-{p.bed_end}",
                    "bedfeet": bf,
                    "status": p.status,
                    "planned_yield": planned_yield,
                    "actual_yield": actual_yield,
                    "yield_variance": yield_variance,
                    "yield_variance_pct": yield_variance_pct,
                    "planned_yield_per_bf": planned_yield_per_bf,
                    "actual_yield_per_bf": actual_yield_per_bf,
                    "reference_yield_per_bf": p.crop_season.total_yield_per_bedfoot,
                    "harvest_unit": p.crop.harvest_unit,
                    "plant_variance_days": plant_variance_days,
                    "harvest_variance_days": harvest_variance_days,
                    "actual_hours": actual_hours,
                    "harvest_rate": (
                        actual_yield / actual_hours if actual_yield and actual_hours else None
                    ),
                    "has_actuals": actual_yield is not None,
                }
            )

        # Summary
        with_actuals = [r for r in rows if r["has_actuals"]]

        overperformers = [
            r for r in with_actuals if r["yield_variance_pct"] and r["yield_variance_pct"] > 10
        ]
        underperformers = [
            r for r in with_actuals if r["yield_variance_pct"] and r["yield_variance_pct"] < -15
        ]

        ctx.update(
            {
                "rows": rows,
                "total_plantings": len(rows),
                "with_actuals": len(with_actuals),
                "overperformers": sorted(
                    overperformers, key=lambda r: r["yield_variance_pct"], reverse=True
                )[:10],
                "underperformers": sorted(underperformers, key=lambda r: r["yield_variance_pct"])[
                    :10
                ],
            }
        )
        ctx.update(
            self.build_analyze_context(
                year_obj,
                page_subtitle="Per-planting yield and timing comparisons against the original plan.",
                empty_message=(
                    "No planting comparisons are available yet for this planning year."
                    if not rows
                    else ""
                ),
            )
        )
        return ctx


class CropMapPrintView(ReportContextMixin, TemplateView):
    """Printable crop map — optimized for 11×17 landscape."""

    template_name = "reports/crop_map_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        year = year_obj.year

        week_num = self.resolve_week(kwargs.get("week"))
        week_date = self.week_window(year, week_num)[0]

        blocks = Block.objects.all().order_by("walk_route_order", "name")

        # Get all plantings for the entire season (not just this week)
        # The print map shows the full year in a grid
        all_plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
            )
            .exclude(status__in=self.excluded_statuses)
            .select_related("crop", "block")
            .order_by("block__name", "bed_start", "planned_plant_date")
        )

        # Week range to display — configurable but default full season
        display_start, display_end = self.parse_week_range(self.request, 14, 45)
        weeks = list(range(display_start, display_end + 1))

        # Build block rows — each block gets multiple rows if beds overlap
        block_rows = []

        for block in blocks:
            block_plantings = [p for p in all_plantings if p.block_id == block.id]

            if not block_plantings:
                block_rows.append(
                    {
                        "block": block,
                        "rows": [[None] * len(weeks)],
                        "is_empty": True,
                    }
                )
                continue

            # Assign each planting to a row (track bed occupancy per week)
            rows = []

            for p in block_plantings:
                plant_wk = p.planned_plant_date.isocalendar()[1]
                end_wk = p.planned_last_harvest_date.isocalendar()[1]

                # Find a row where this planting's weeks don't overlap
                placed = False
                for row in rows:
                    # Check if any of our weeks are already occupied
                    conflict = False
                    for wk_idx, wk in enumerate(weeks):
                        if plant_wk <= wk <= end_wk:
                            if row[wk_idx] is not None:
                                conflict = True
                                break

                    if not conflict:
                        # Place in this row
                        for wk_idx, wk in enumerate(weeks):
                            if plant_wk <= wk <= end_wk:
                                row[wk_idx] = p
                        placed = True
                        break

                if not placed:
                    # Start a new row
                    new_row = [None] * len(weeks)
                    for wk_idx, wk in enumerate(weeks):
                        if plant_wk <= wk <= end_wk:
                            new_row[wk_idx] = p
                    rows.append(new_row)

            if not rows:
                rows = [[None] * len(weeks)]

            block_rows.append(
                {
                    "block": block,
                    "rows": rows,
                    "is_empty": False,
                    "num_rows": len(rows),
                }
            )

        # Week labels with month markers
        week_labels = []
        prev_month = None
        for wk in weeks:
            monday = Week(year, wk).monday()
            month = monday.strftime("%b")
            is_month_start = month != prev_month
            prev_month = month
            week_labels.append(
                {
                    "num": wk,
                    "date": monday,
                    "month": month,
                    "is_month_start": is_month_start,
                }
            )

        ctx.update(
            {
                "year": year_obj,
                "week_num": week_num,
                "week_date": week_date,
                "weeks": weeks,
                "week_labels": week_labels,
                "block_rows": block_rows,
                "display_start": display_start,
                "display_end": display_end,
                "num_weeks": len(weeks),
                "today": date.today(),
                "crop_colors": CROP_MAP_PRINT_COLORS,
                "col_width": 22,
            }
        )
        return ctx


class CropMapWeekByBedGridView(ReportContextMixin, TemplateView):
    """503-style calendar week by bed occupancy grid."""

    template_name = "reports/crop_map_week_by_bed.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        week_start, week_end = self.parse_week_range(self.request, 1, 26)

        service = CropMapOccupancyService(year_obj)
        grid = service.get_week_by_bed_grid(week_start=week_start, week_end=week_end)
        ctx.update(
            {
                "year": year_obj,
                "week_start": week_start,
                "week_end": week_end,
                "weeks": grid["weeks"],
                "rows": grid["rows"],
            }
        )
        return ctx


class CropMapWeekByBlockGridView(ReportContextMixin, TemplateView):
    """503-style calendar week by block occupancy grid."""

    template_name = "reports/crop_map_week_by_block.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        week_start, week_end = self.parse_week_range(self.request, 1, 52)

        service = CropMapOccupancyService(year_obj)
        grid = service.get_week_by_block_grid(week_start=week_start, week_end=week_end)
        ctx.update(
            {
                "year": year_obj,
                "week_start": week_start,
                "week_end": week_end,
                "weeks": grid["weeks"],
                "rows": grid["rows"],
            }
        )
        return ctx


class CropMapSuccessionsByBlockView(ReportContextMixin, TemplateView):
    """503-style succession group view by block."""

    template_name = "reports/crop_map_successions_by_block.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.resolve_planning_year(status_priority=("active", "complete"))
        service = CropMapOccupancyService(year_obj)
        ctx.update({"year": year_obj, "rows": service.get_successions_by_block()})
        return ctx
