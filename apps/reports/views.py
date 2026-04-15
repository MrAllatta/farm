"""reports/views.py"""

from django.views.generic import TemplateView
from django.db.models import Sum, F, Q
from datetime import date, timedelta
from isoweek import Week
import math

from planning.models import HarvestEvent, Planting
from core.planning_year import resolve_current_planning_year
from sales.models import SalesEvent, QuickSalesEntry
from reference.models import Block, SalesChannel, CropSalesFormat
from decimal import Decimal


class WeeklySchedulePrintView(TemplateView):
    """Weekly Schedule Print"""

    template_name = "reports/harvest_list_print.html"


class PackListPrintView(TemplateView):
    """Pack list print"""

    template_name = "reports/harvest_list_print.html"


class ExportArchiveView(TemplateView):
    """Export archive"""

    template_name = "reports/harvest_list_print.html"


class ExportCSVView(TemplateView):
    """Export csvs"""

    template_name = "reports/harvest_list_print.html"


class SeedOrderReportView(TemplateView):
    """View seed order reports"""

    template_name = "reports/harvest_list_print.html"


class NurserySchedulePrintView(TemplateView):
    """Generate print-ready nursery schedule."""

    template_name = "reports/harvest_list_print.html"


class HarvestListPrintView(TemplateView):
    """Generates a print-ready harvest list for a given week."""

    template_name = "reports/harvest_list_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year()
        week_num = kwargs["week"]
        year = year_obj.year

        week_monday = Week(year, week_num).monday()
        week_sunday = week_monday + timedelta(days=6)

        events = (
            HarvestEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .exclude(planting__status__in=["skipped", "failed"])
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
                "year": year_obj,
                "week_num": week_num,
                "harvest_day": harvest_day,
                "items": items,
                "bin_totals": sorted(bin_totals.items()),
                "total_bins": sum(bin_totals.values()),
                "tools_needed": sorted(tools_needed),
                "total_items": len(items),
            }
        )
        return ctx


class RevenueProjectionView(TemplateView):
    """Project weekly revenue from planned plantings × sales formats."""

    template_name = "reports/revenue_projection.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year()
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


class CropPerformanceView(TemplateView):
    """Per-crop analysis: yield, revenue, $/bedfoot."""

    template_name = "reports/crop_performance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year(status_priority=("active", "complete"))

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
                "year": year_obj,
                "crops": performance,
                "total_revenue": total_revenue,
                "total_bedfeet": total_bedfeet,
                "avg_revenue_per_bf": (total_revenue / total_bedfeet if total_bedfeet else 0),
                "num_crops": len(performance),
            }
        )
        return ctx


class ChannelPerformanceView(TemplateView):
    """Revenue by channel — weekly, monthly, annual vs target."""

    template_name = "reports/channel_performance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year(status_priority=("active", "complete"))
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
                "year": year_obj,
                "channels": channel_data,
                "total_ytd": total_ytd,
                "total_target_ytd": total_target_ytd,
                "total_annual_target": total_annual_target,
                "total_ytd_gap": total_ytd - total_target_ytd,
            }
        )
        return ctx


class SeasonSummaryView(TemplateView):
    """End-of-season overview — the post-season analysis dashboard."""

    template_name = "reports/season_summary.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year(status_priority=("active", "complete"))
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
                "year": year_obj,
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
        return ctx


class CropMapView(TemplateView):
    """Spatial farm view showing what's planted where."""

    template_name = "reports/crop_map.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year()
        year = year_obj.year

        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_date = Week(year, week_num).monday()

        blocks = Block.objects.all().order_by("walk_route_order", "name")

        # Get all plantings active during this week
        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
                planned_plant_date__lte=week_date + timedelta(days=6),
                planned_last_harvest_date__gte=week_date,
            )
            .exclude(status__in=["skipped"])
            .select_related("crop", "block")
            .order_by("block__name", "bed_start")
        )

        # Build map: block → list of bed segments
        block_maps = []

        for block in blocks:
            block_plantings = [p for p in plantings if p.block_id == block.id]

            segments = []
            covered_beds = set()

            for p in block_plantings:
                for bed in range(p.bed_start, p.bed_end + 1):
                    covered_beds.add(bed)

                # Determine status relative to current week
                harvest_start = p.planned_first_harvest_date
                harvest_end = p.planned_last_harvest_date

                if p.status == "failed":
                    status_label = "failed"
                elif week_date > harvest_end:
                    status_label = "finishing"
                elif week_date >= harvest_start:
                    status_label = "harvesting"
                elif week_date >= p.planned_plant_date:
                    status_label = "growing"
                else:
                    status_label = "planned"

                # CSS class for crop type coloring
                crop_type_css = p.crop.crop_type.lower().replace("/", "-").replace(" ", "-")

                segments.append(
                    {
                        "planting": p,
                        "bed_start": p.bed_start,
                        "bed_end": p.bed_end,
                        "bed_count": p.bed_end - p.bed_start + 1,
                        "width_pct": ((p.bed_end - p.bed_start + 1) / block.num_beds * 100),
                        "label": p.crop.name,
                        "sublabel": f"b{p.bed_start}-{p.bed_end}",
                        "status": status_label,
                        "crop_type_css": f"crop-{crop_type_css}",
                    }
                )

            # Find fallow gaps
            all_beds = set(range(1, block.num_beds + 1))
            fallow_beds = sorted(all_beds - covered_beds)

            # Group consecutive fallow beds
            fallow_segments = []
            if fallow_beds:
                start = fallow_beds[0]
                prev = fallow_beds[0]
                for bed in fallow_beds[1:]:
                    if bed == prev + 1:
                        prev = bed
                    else:
                        fallow_segments.append(
                            {
                                "bed_start": start,
                                "bed_end": prev,
                                "bed_count": prev - start + 1,
                                "width_pct": (prev - start + 1) / block.num_beds * 100,
                                "label": "fallow",
                                "status": "fallow",
                                "crop_type_css": "",
                            }
                        )
                        start = bed
                        prev = bed
                fallow_segments.append(
                    {
                        "bed_start": start,
                        "bed_end": prev,
                        "bed_count": prev - start + 1,
                        "width_pct": (prev - start + 1) / block.num_beds * 100,
                        "label": "fallow",
                        "status": "fallow",
                        "crop_type_css": "",
                    }
                )

            # Merge and sort all segments by bed position
            all_segments = segments + fallow_segments
            all_segments.sort(key=lambda s: s["bed_start"])

            block_maps.append(
                {
                    "block": block,
                    "segments": all_segments,
                    "fallow_beds": len(fallow_beds),
                    "fallow_bedfeet": len(fallow_beds) * block.bedfeet_per_bed,
                    "utilization_pct": (
                        len(covered_beds) / block.num_beds * 100 if block.num_beds else 0
                    ),
                }
            )

        # Group blocks by type
        field_maps = [bm for bm in block_maps if bm["block"].block_type == "field"]
        tunnel_maps = [bm for bm in block_maps if bm["block"].block_type == "high_tunnel"]
        greenhouse_maps = [bm for bm in block_maps if bm["block"].block_type == "greenhouse"]

        # Summary stats
        total_fallow_bf = sum(bm["fallow_bedfeet"] for bm in block_maps)
        total_bf = sum(bm["block"].total_bedfeet for bm in block_maps)

        ctx.update(
            {
                "year": year_obj,
                "week_num": week_num,
                "week_date": week_date,
                "field_maps": field_maps,
                "tunnel_maps": tunnel_maps,
                "greenhouse_maps": greenhouse_maps,
                "total_fallow_bf": total_fallow_bf,
                "total_bf": total_bf,
                "overall_utilization": (
                    (total_bf - total_fallow_bf) / total_bf * 100 if total_bf else 0
                ),
            }
        )
        return ctx


class BlockUtilizationView(TemplateView):
    """Per-block analysis: weeks in use, revenue, $/bf."""

    template_name = "reports/block_utilization.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year(status_priority=("active", "complete"))

        blocks = Block.objects.all().order_by("walk_route_order", "name")

        block_data = []

        for block in blocks:
            plantings = (
                Planting.objects.filter(
                    planning_year=year_obj,
                    block=block,
                )
                .exclude(status__in=["skipped"])
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
                "year": year_obj,
                "blocks": block_data,
                "total_revenue": total_revenue,
                "total_bf": total_bf,
                "avg_utilization": avg_utilization,
                "avg_revenue_per_bf": (total_revenue / total_bf if total_bf else 0),
            }
        )
        return ctx


class PlanVsActualView(TemplateView):
    """Per-planting comparison of plan to reality."""

    template_name = "reports/plan_vs_actual.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year(status_priority=("active", "complete"))

        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
            )
            .exclude(status="skipped")
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
                "year": year_obj,
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
        return ctx


class CropMapPrintView(TemplateView):
    """Printable crop map — optimized for 11×17 landscape."""

    template_name = "reports/crop_map_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = resolve_current_planning_year()
        year = year_obj.year

        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_date = Week(year, week_num).monday()

        blocks = Block.objects.all().order_by("walk_route_order", "name")

        # Get all plantings for the entire season (not just this week)
        # The print map shows the full year in a grid
        all_plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
            )
            .exclude(status__in=["skipped", "revised"])
            .select_related("crop", "block")
            .order_by("block__name", "bed_start", "planned_plant_date")
        )

        # Week range to display — configurable but default full season
        display_start = int(self.request.GET.get("start", 14))
        display_end = int(self.request.GET.get("end", 45))
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
            }
        )
        return ctx
