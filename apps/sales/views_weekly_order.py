"""Weekly channel order workflow (SP-5 / SP-6).

Kept in a submodule to keep ``sales.views`` smaller; imported from ``sales.views``.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import TemplateView

from core.planning_year import get_effective_planning_year
from planning.models import HarvestEvent, PlanningYear
from reference.models import CropSalesFormat, SalesChannel
from reference.sales_rollups import plan_events_without_shadowed_rollups
from reports.mixins import ReportContextMixin

from .models import SalesEvent


class WeeklyChannelOrderView(ReportContextMixin, TemplateView):
    """One ISO week + channel: planned demand, harvest supply, prior-year actuals, editable plan."""

    template_name = "sales/weekly_channel_order.html"

    def dispatch(self, request, *args, **kwargs):
        self.year_obj = get_effective_planning_year(request)
        if not self.year_obj:
            messages.error(request, "No active planning year configured.")
            return redirect("planning:matrix")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)

        channel = get_object_or_404(SalesChannel, pk=kwargs["channel_id"])
        week_num = self.resolve_week(kwargs.get("week"))
        week_monday, week_sunday = self.week_window(self.year_obj.year, week_num)
        products = CropSalesFormat.objects.filter(is_active=True).select_related("crop")
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
        week_monday, week_sunday = self.week_window(cal_year, week_num)

        products = (
            CropSalesFormat.objects.filter(is_active=True)
            .select_related("crop")
            .order_by("crop__crop_type", "crop__name", "product_name")
        )

        all_plan = list(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
            ).select_related("product", "channel", "channel__category", "sales_category")
        )
        demand_by_product_week = defaultdict(Decimal)
        for row in plan_events_without_shadowed_rollups(all_plan):
            wk = row.sale_date.isocalendar()[1]
            if wk == week_num and row.product_id:
                demand_by_product_week[row.product_id] += row.planned_quantity or Decimal("0")

        harvest_by_crop = defaultdict(Decimal)
        for he in HarvestEvent.objects.filter(
            planting__planning_year=self.year_obj,
            planned_date__gte=week_monday,
            planned_date__lte=week_sunday,
        ).exclude(planting__status__in=["skipped", "failed", "revised"]).select_related("planting"):
            harvest_by_crop[he.planting.crop_id] += he.planned_quantity or Decimal("0")

        historical = []
        prior_years = PlanningYear.objects.filter(year__lt=cal_year).order_by("-year")[:6]
        for py in prior_years:
            hm, hs = self.week_window(py.year, week_num)
            actual_qs = SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.ACTUAL,
                channel=channel,
                sale_date__gte=hm,
                sale_date__lte=hs,
            ).select_related("product")
            by_product: dict[int, SalesEvent] = {}
            for row in actual_qs:
                if row.product_id:
                    by_product[row.product_id] = row
            historical.append(
                {
                    "calendar_year": py.year,
                    "week_monday": hm,
                    "by_product": by_product,
                    "empty": not by_product,
                }
            )

        order_rows = []
        for product in products:
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
            demand_all = demand_by_product_week.get(product.id, Decimal("0"))
            shortage = demand_all > supply_sale_units + Decimal("0.0001")
            hist_cells = []
            for block in historical:
                ev = block["by_product"].get(product.id)
                hist_cells.append(
                    {
                        "calendar_year": block["calendar_year"],
                        "actual_qty": ev.actual_quantity if ev else None,
                        "actual_revenue": ev.actual_revenue if ev else None,
                    }
                )
            order_rows.append(
                {
                    "product": product,
                    "channel_planned_qty": planned_qty,
                    "demand_all_channels": demand_all,
                    "supply_sale_units": supply_sale_units,
                    "shortage": shortage,
                    "historical_cells": hist_cells,
                }
            )

        ctx.update(
            {
                "year": self.year_obj,
                "channel": channel,
                "channels": SalesChannel.objects.order_by("allocation_priority", "name"),
                "week_num": week_num,
                "week_monday": week_monday,
                "week_sunday": week_sunday,
                "order_rows": order_rows,
                "historical_year_columns": historical,
                "has_any_historical": any(not h["empty"] for h in historical),
            }
        )
        ctx.update(self.week_navigation(week_num))
        return ctx
