"""core/views.py"""

from datetime import date, timedelta
from django.views.generic import TemplateView
from django.shortcuts import redirect
from django.contrib import messages
from django.db.models import Sum, Count, Q
from isoweek import Week

from planning.models import PlanningYear, Planting, NurseryEvent, HarvestEvent
from operations.models import InventoryLedger
from sales.models import SalesEvent, QuickSalesEntry
from reference.models import SalesChannel

from django.views.generic import FormView
from django import forms


class DashboardView(TemplateView):
    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = PlanningYear.objects.filter(status__in=["planning", "active"]).first()

        if not year_obj:
            ctx["no_year"] = True
            return ctx

        today = date.today()
        current_week = today.isocalendar()[1]
        year = year_obj.year

        week_monday = Week(year, current_week).monday()
        week_sunday = week_monday + timedelta(days=6)
        next_monday = week_sunday + timedelta(days=1)
        next_sunday = next_monday + timedelta(days=6)

        # ── This Week's Tasks ──

        # Nursery events this week
        nursery_this_week = (
            NurseryEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .select_related("planting__crop", "planting__block")
            .order_by("event_type", "planned_date")
        )

        nursery_pending = nursery_this_week.filter(actual_date__isnull=True)
        nursery_done = nursery_this_week.filter(actual_date__isnull=False)

        # Harvest events this week
        harvests_this_week = (
            HarvestEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .exclude(planting__status__in=["skipped", "failed"])
            .select_related("planting__crop", "planting__block")
        )

        harvest_count = harvests_this_week.count()
        harvest_recorded = harvests_this_week.filter(actual_quantity__isnull=False).count()

        # Transplants this week (nursery events of type 'transplant')
        transplants_this_week = nursery_this_week.filter(event_type="transplant")

        # ── Revenue ──

        # This week's sales (actual or quick)
        week_sales = SalesEvent.objects.filter(
            sale_date__gte=week_monday,
            sale_date__lte=week_sunday,
            actual_revenue__isnull=False,
        ).aggregate(total=Sum("actual_revenue"))["total"]

        if not week_sales:
            week_sales = QuickSalesEntry.objects.filter(
                sale_date__gte=week_monday,
                sale_date__lte=week_sunday,
            ).aggregate(total=Sum("total_cash") + Sum("total_card"))["total"]

        # Week target
        week_target = (
            SalesChannel.objects.filter(
                start_week__lte=current_week,
                end_week__gte=current_week,
            ).aggregate(total=Sum("weekly_target"))["total"]
            or 0
        )

        # YTD revenue
        ytd_sales = (
            SalesEvent.objects.filter(
                sale_date__year=year,
                actual_revenue__isnull=False,
            ).aggregate(total=Sum("actual_revenue"))["total"]
            or 0
        )

        quick_ytd = QuickSalesEntry.objects.filter(
            sale_date__year=year,
        ).aggregate(
            cash=Sum("total_cash"),
            card=Sum("total_card"),
        )
        ytd_sales += (quick_ytd["cash"] or 0) + (quick_ytd["card"] or 0)

        annual_target = sum(ch.annual_target for ch in SalesChannel.objects.all())

        # ── Planting Stats ──

        planting_counts = (
            Planting.objects.filter(
                planning_year=year_obj,
            )
            .values("status")
            .annotate(count=Count("id"))
        )

        status_map = {item["status"]: item["count"] for item in planting_counts}

        total_plantings = sum(status_map.values())
        active_plantings = (
            status_map.get("planted", 0)
            + status_map.get("growing", 0)
            + status_map.get("harvesting", 0)
        )

        # ── Storage Inventory ──

        # Current inventory balances
        from django.db.models import Max

        storage_crops = (
            InventoryLedger.objects.filter(
                event_date__lte=today,
            )
            .values("crop__name")
            .annotate(
                latest_date=Max("event_date"),
            )
        )

        # Get the running balance at the latest entry for each crop
        inventory_summary = []
        for sc in storage_crops:
            latest = (
                InventoryLedger.objects.filter(
                    crop__name=sc["crop__name"],
                    event_date=sc["latest_date"],
                )
                .order_by("-created_at")
                .first()
            )

            if latest and latest.running_balance > 0:
                inventory_summary.append(
                    {
                        "crop": sc["crop__name"],
                        "balance": latest.running_balance,
                        "expiry": latest.expiry_date,
                        "weeks_left": (
                            (latest.expiry_date - today).days // 7 if latest.expiry_date else None
                        ),
                    }
                )

        inventory_summary.sort(key=lambda x: x.get("weeks_left") or 999)

        # ── Next Week Preview ──

        nursery_next_week = (
            NurseryEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=next_monday,
                planned_date__lte=next_sunday,
            )
            .select_related("planting__crop")
            .order_by("event_type")
        )

        # First harvests starting next week
        first_harvests_next = (
            Planting.objects.filter(
                planning_year=year_obj,
                planned_first_harvest_date__gte=next_monday,
                planned_first_harvest_date__lte=next_sunday,
            )
            .exclude(status__in=["skipped", "failed"])
            .select_related("crop", "block")
        )

        # Last harvests ending next week
        last_harvests_next = (
            Planting.objects.filter(
                planning_year=year_obj,
                planned_last_harvest_date__gte=next_monday,
                planned_last_harvest_date__lte=next_sunday,
            )
            .exclude(status__in=["skipped", "failed"])
            .select_related("crop", "block")
        )

        ctx.update(
            {
                "year": year_obj,
                "week_num": current_week,
                "week_monday": week_monday,
                # This week tasks
                "nursery_pending": nursery_pending,
                "nursery_done": nursery_done,
                "transplants_this_week": transplants_this_week,
                "harvest_count": harvest_count,
                "harvest_recorded": harvest_recorded,
                # Revenue
                "week_sales": week_sales or 0,
                "week_target": week_target,
                "ytd_sales": ytd_sales,
                "annual_target": annual_target,
                "ytd_pct": (ytd_sales / annual_target * 100 if annual_target else 0),
                # Plantings
                "total_plantings": total_plantings,
                "active_plantings": active_plantings,
                "status_map": status_map,
                # Storage
                "inventory_summary": inventory_summary[:8],
                "inventory_warnings": [
                    i for i in inventory_summary if i.get("weeks_left") and i["weeks_left"] < 4
                ],
                # Next week
                "nursery_next_week": nursery_next_week,
                "first_harvests_next": first_harvests_next,
                "last_harvests_next": last_harvests_next,
            }
        )
        return ctx


class ClonePlanForm(forms.Form):
    source_year = forms.IntegerField(widget=forms.HiddenInput)
    target_year = forms.IntegerField(
        label="New planning year",
        min_value=2020,
        max_value=2050,
    )
    use_actual_yields = forms.BooleanField(
        required=False,
        initial=True,
        label="Use actual yields as planning baseline (where available)",
        help_text=(
            "If checked, crops where you have actual harvest data will "
            "use that as the yield projection instead of the reference value."
        ),
    )
    include_failed = forms.BooleanField(
        required=False,
        initial=False,
        label="Include failed plantings",
        help_text="Usually you want to leave these out and reassess.",
    )


class ClonePlanUIView(FormView):
    template_name = "core/clone_plan.html"
    form_class = ClonePlanForm

    def get_initial(self):
        source_year = int(self.kwargs.get("source_year"))
        return {
            "source_year": source_year,
            "target_year": source_year + 1,
            "use_actual_yields": True,
        }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        source_year = int(self.kwargs.get("source_year"))

        try:
            source = PlanningYear.objects.get(year=source_year)
        except PlanningYear.DoesNotExist:
            ctx["error"] = f"Year {source_year} not found."
            return ctx

        # Preview what will be cloned
        plantings = Planting.objects.filter(
            planning_year=source,
        ).exclude(status="skipped")

        # Check rotation violations
        from core.models import RotationRule, RotationHistory

        violations = []

        for p in plantings:
            family = p.crop.botanical_family
            if not family:
                continue
            rule = RotationRule.objects.filter(botanical_family=family).first()
            if not rule:
                continue

            recent = RotationHistory.objects.filter(
                block=p.block,
                botanical_family=family,
                year__gte=source_year + 1 - rule.min_gap_years,
                year__lt=source_year + 1,
            ).exists()

            if recent:
                violations.append(
                    {
                        "crop": p.crop.name,
                        "block": p.block.name,
                        "family": family,
                        "min_gap": rule.min_gap_years,
                    }
                )

        # Count by status
        status_counts = {}
        for p in plantings:
            status_counts[p.status] = status_counts.get(p.status, 0) + 1

        total_bedfeet = sum(p.planned_bedfeet for p in plantings)

        ctx.update(
            {
                "source": source,
                "target_year": source_year + 1,
                "num_plantings": plantings.count(),
                "status_counts": status_counts,
                "total_bedfeet": total_bedfeet,
                "rotation_violations": violations,
                "num_violations": len(violations),
            }
        )
        return ctx

    def form_valid(self, form):
        from django.core.management import call_command
        from io import StringIO

        source_year = form.cleaned_data["source_year"]
        target_year = form.cleaned_data["target_year"]
        include_actuals = form.cleaned_data["use_actual_yields"]

        # Run the clone command programmatically
        out = StringIO()

        try:
            call_command(
                "clone_plan",
                source_year,
                target_year,
                include_actuals=include_actuals,
                stdout=out,
                stderr=out,
            )

            output = out.getvalue()
            messages.success(
                self.request,
                f"Successfully created {target_year} plan from {source_year}. "
                f"Review rotation warnings in the planning matrix.",
            )

            # Store output in session to display
            self.request.session["clone_output"] = output

        except Exception as e:
            messages.error(self.request, f"Error cloning plan: {str(e)}")
            return self.form_invalid(form)

        return redirect("planning:matrix")


class CompleteSeasonView(TemplateView):
    """Mark a planning year as complete and update rotation history."""

    def post(self, request, **kwargs):
        year_obj = PlanningYear.objects.filter(status="active").first()

        if not year_obj:
            messages.error(request, "No active planning year found.")
            return redirect("core:dashboard")

        # Update rotation history from completed plantings
        completed = Planting.objects.filter(
            planning_year=year_obj,
            status__in=["complete", "harvesting"],
        ).select_related("crop", "block")

        rotation_updated = 0
        seen = set()

        for p in completed:
            family = p.crop.botanical_family
            if not family:
                continue

            key = (p.block_id, family)
            if key in seen:
                continue
            seen.add(key)

            RotationHistory.objects.update_or_create(
                block=p.block,
                year=year_obj.year,
                defaults={
                    "botanical_family": family,
                    "notes": f"Auto-recorded at season completion",
                },
            )
            rotation_updated += 1

        # Archive any remaining active plantings
        still_active = Planting.objects.filter(
            planning_year=year_obj,
            status__in=["planted", "growing", "harvesting"],
        )
        still_active.update(status="complete")

        # Mark year complete
        year_obj.status = "complete"
        year_obj.save()

        messages.success(
            request,
            f"{year_obj.year} season archived. "
            f"Updated rotation history for {rotation_updated} block/family "
            f"combinations. You can now clone this plan for "
            f"{year_obj.year + 1}.",
        )

        return redirect("reports:season_summary")
