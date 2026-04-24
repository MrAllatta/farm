# operations/views.py

from django.views.generic import TemplateView, FormView, RedirectView
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q, Max, Subquery, OuterRef, Sum
from django.http import Http404, HttpResponse
from django.urls import reverse
from datetime import date, timedelta
from isoweek import Week
from django import forms
from reference.models import CropInfo, CropSalesFormat, ProductRecipe, SalesChannel
from operations.models import InventoryLedger, FieldWalkNote, PackAllocation, PackBatch, PackBatchComponent
from sales.models import SalesEvent
from planning.models import HarvestEvent, NurseryEvent, Planting, PlantingStatus
from core.planning_year import get_effective_planning_year
from decimal import Decimal

from operations.services.field_walk_cascade import apply_yield_adjustment_to_future_harvests
from operations.services import week_ops as week_ops_service
from operations.planting_display import format_planting_display_id, planting_schedule_chip_css_class


def _weekops_header_context(phase: str, iso_week: int, year_obj, wctx: dict) -> dict:
    """Context keys expected by ``operations/_weekops/_week_header.html``."""
    w = max(1, min(52, int(iso_week)))
    return {
        "weekops_phase": phase,
        "week_num": w,
        "prev_week": w - 1 if w > 1 else 52,
        "next_week": w + 1 if w < 52 else 1,
        "year": year_obj,
        "week_monday": wctx["week_monday"],
        "week_sunday": wctx["week_sunday"],
        "progress": wctx["progress"],
    }


class OperationsPlanningYearMixin:
    """Require an active planning year for operations views."""

    year_obj = None

    def dispatch(self, request, *args, **kwargs):
        self.year_obj = get_effective_planning_year(request)
        if not self.year_obj:
            messages.error(request, "No active planning year configured.")
            return redirect("planning:matrix")
        return super().dispatch(request, *args, **kwargs)


class InventoryHarvestInView(TemplateView):
    """Harvest to inventory: review auto ledger rows and post manual adjustments."""

    template_name = "operations/inventory_harvest_in.html"

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        harvest_event = get_object_or_404(
            HarvestEvent.objects.select_related("planting", "planting__crop"),
            pk=kwargs["harvest_event_id"],
        )
        raw = (request.POST.get("adjustment_quantity") or "").strip()
        notes = (request.POST.get("notes") or "").strip()
        try:
            qty = Decimal(raw)
        except Exception:
            messages.warning(request, "Invalid adjustment quantity.")
            return redirect("operations:inventory_harvest_in", harvest_event_id=harvest_event.pk)
        if qty == 0:
            messages.warning(request, "Enter a non-zero adjustment (use negative to remove inventory).")
            return redirect("operations:inventory_harvest_in", harvest_event_id=harvest_event.pk)

        from operations.services.inventory_ledger_sync import append_ledger_entry

        crop = harvest_event.planting.crop
        append_ledger_entry(
            crop,
            date.today(),
            "adjustment",
            qty,
            harvest_event=harvest_event,
            notes=f"Manual harvest/inventory adjustment. {notes}".strip(),
        )
        messages.success(request, f"Recorded adjustment of {qty} {crop.harvest_unit} for {crop.name}.")
        return redirect("operations:inventory_harvest_in", harvest_event_id=harvest_event.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        harvest_event = get_object_or_404(
            HarvestEvent.objects.select_related(
                "planting",
                "planting__crop",
                "planting__block",
                "planting__planning_year",
            ),
            pk=kwargs["harvest_event_id"],
        )
        crop = harvest_event.planting.crop
        latest_ledger = (
            InventoryLedger.objects.filter(crop=crop).order_by("-event_date", "-created_at").first()
        )
        recent_ledger_entries = InventoryLedger.objects.filter(crop=crop).order_by(
            "-event_date", "-created_at"
        )[:10]

        ctx.update(
            {
                "harvest_event": harvest_event,
                "planting": harvest_event.planting,
                "crop": crop,
                "latest_ledger": latest_ledger,
                "recent_ledger_entries": recent_ledger_entries,
                "current_balance": latest_ledger.running_balance if latest_ledger else Decimal("0"),
            }
        )
        return ctx


class FieldWalkNoteView(TemplateView):
    """Field walk notes"""

    template_name = "operations/field_walk_note.html"

    def _get_planting_for_request(self, request, pk):
        year_obj = get_effective_planning_year(request)
        if not year_obj:
            raise Http404("No active planning year")
        return get_object_or_404(
            Planting.objects.select_related("crop", "crop_season", "block", "planning_year"),
            pk=pk,
            planning_year=year_obj,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        planting = self._get_planting_for_request(self.request, kwargs["pk"])
        recent_notes = planting.field_walk_notes.order_by("-walk_date")[:12]
        latest_note = recent_notes[0] if recent_notes else None

        today = date.today()
        ctx.update(
            {
                "planting": planting,
                "recent_notes": recent_notes,
                "latest_note": latest_note,
                "today": today,
                "planting_display_id": format_planting_display_id(planting.pk),
                "schedule_chip_class": planting_schedule_chip_css_class(
                    planting.planned_plant_date, planting.actual_plant_date, today
                ),
            }
        )
        return ctx

    def post(self, request, **kwargs):
        """Append a single field-walk note for this planting (same semantics as batch field walk)."""
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)

        planting = self._get_planting_for_request(request, kwargs["pk"])
        year_obj = get_effective_planning_year(request)
        today = date.today()

        condition = (request.POST.get("condition") or "").strip()
        if not condition:
            messages.warning(request, "Select a condition before saving.")
            return redirect("operations:field_walk", pk=planting.pk)

        notes_text = request.POST.get("notes", "")
        yield_pct_raw = request.POST.get("yield_adjust", "100")
        adjusted_harvest = request.POST.get("adj_harvest", "")

        try:
            yield_pct = int(yield_pct_raw)
        except ValueError:
            yield_pct = 100

        fw = FieldWalkNote.objects.create(
            planting=planting,
            walk_date=today,
            condition=condition,
            yield_adjust_pct=yield_pct,
            notes=notes_text,
        )

        n_adj = apply_yield_adjustment_to_future_harvests(planting, yield_pct)
        if n_adj:
            messages.info(request, f"Adjusted {n_adj} planned harvest week(s) for yield change.")

        if adjusted_harvest:
            try:
                adj_week = int(adjusted_harvest)
                fw.adjusted_first_harvest_date = Week(year_obj.year, adj_week).monday()
                fw.save()
            except (ValueError, TypeError):
                pass

        if condition == "failed":
            planting.status = "failed"
            planting.notes += f"\nFailed: {today} — {notes_text}"
            planting.save()
        elif planting.status == PlantingStatus.PLANTED:
            planting.status = PlantingStatus.GROWING
            planting.save(update_fields=["status"])

        messages.success(request, "Field walk note saved.")
        return redirect("operations:field_walk", pk=planting.pk)


class PlantingHarvestEntryView(TemplateView):
    """Planting Harvest Entry"""

    template_name = "operations/planting_harvest_entry.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        planting = get_object_or_404(
            Planting.objects.select_related("crop", "crop_season", "block", "planning_year"),
            pk=kwargs["pk"],
        )
        harvest_events = planting.harvest_events.order_by("planned_date")
        recorded_count = harvest_events.filter(actual_quantity__isnull=False).count()

        ctx.update(
            {
                "planting": planting,
                "harvest_events": harvest_events,
                "event_count": harvest_events.count(),
                "recorded_count": recorded_count,
            }
        )
        return ctx


class WeeklyHarvestEntryView(OperationsPlanningYearMixin, TemplateView):
    """Batch harvest entry for a given week (unified week-ops surface)."""

    template_name = "operations/harvest_entry.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_num = max(1, min(52, int(week_num)))
        today = date.today()
        wctx = week_ops_service.week_context(
            self.year_obj, week_num, today=today, mode="harvest_entry"
        )
        blocks_out = {}
        total_bins = Decimal("0")
        for group in wctx["blocks"]:
            block_name = group["block"].name
            if block_name not in blocks_out:
                blocks_out[block_name] = []
            for prow in group["plantings"]:
                p = prow["planting"]
                crop = p.crop
                for he in prow["harvest_events_this_week"]:
                    tb = week_ops_service.target_bins_for_event(he)
                    if tb is not None:
                        total_bins += Decimal(str(tb))
                    blocks_out[block_name].append(
                        {
                            "event": he,
                            "planting": p,
                            "prow": prow,
                            "crop_name": crop.name,
                            "block": block_name,
                            "beds": f"{p.bed_start}-{p.bed_end}",
                            "target_qty": he.planned_quantity,
                            "units": he.planned_units,
                            "bin_type": crop.harvest_bin,
                            "units_per_bin": crop.units_per_bin,
                            "target_bins": tb,
                            "has_actual": he.actual_quantity is not None,
                            "actual_qty": he.actual_quantity,
                            "actual_bins": he.actual_bins,
                            "inventory_balance": week_ops_service.inventory_balance_for_crop(
                                crop.id
                            ),
                            "cooler_in_url": reverse(
                                "operations:inventory_harvest_in",
                                kwargs={"harvest_event_id": he.id},
                            ),
                        }
                    )

        total_items = wctx["progress"]["total_events"]
        recorded = wctx["progress"]["recorded_events"]
        ctx.update(
            _weekops_header_context("record", week_num, self.year_obj, wctx)
        )
        week_rollup_list = sorted(
            wctx["week_rollup_by_crop"].values(),
            key=lambda r: r["crop"].name.lower(),
        )
        ctx.update(
            {
                "weekops": wctx,
                "week_rollup_by_crop": wctx["week_rollup_by_crop"],
                "week_rollup_list": week_rollup_list,
                "crop_variance": week_ops_service.crop_variance_for_week(
                    self.year_obj, week_num
                ),
                "blocks": blocks_out,
                "total_items": total_items,
                "recorded": recorded,
                "total_bins": float(total_bins),
            }
        )
        return ctx

    def post(self, request, **kwargs):
        """Handle batch harvest entry submission."""
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)

        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_num = max(1, min(52, int(week_num)))
        today = date.today()
        updated = 0
        for key, value in request.POST.items():
            if key.startswith("bins_") and value:
                event_id = key.replace("bins_", "")
                try:
                    he = HarvestEvent.objects.get(
                        id=event_id,
                        planting__planning_year=self.year_obj,
                    )
                    bin_count = float(value)
                    he.record_bins(bin_count)

                    notes_key = f"notes_{event_id}"
                    notes_text = (request.POST.get(notes_key) or "").strip()
                    if notes_text:
                        old = (he.notes or "").strip()
                        line = f"{today.isoformat()}: {notes_text}"
                        he.notes = f"{old}\n{line}".strip() if old else line
                        he.save(update_fields=["notes"])

                    updated += 1
                except (HarvestEvent.DoesNotExist, ValueError):
                    continue

        messages.success(request, f"Recorded {updated} harvest entries.")

        for key, value in request.POST.items():
            if key.startswith("bins_") and value:
                event_id = key.replace("bins_", "")
                try:
                    he = HarvestEvent.objects.get(id=event_id)
                    p = he.planting
                    if p.status in ("planted", "growing"):
                        p.status = "harvesting"
                        if not p.actual_first_harvest_date:
                            p.actual_first_harvest_date = date.today()
                        p.save()
                except HarvestEvent.DoesNotExist:
                    pass

        return redirect("operations:weekops_record", week=week_num)


class InventoryDashboardView(TemplateView):
    """Crop inventory (fresh + storage) with drawdown projections."""

    template_name = "operations/inventory.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        today = date.today()

        # Get current balances per crop
        # Use a subquery to find the latest entry per crop
        from django.db.models import Subquery, OuterRef

        crops_with_inventory = InventoryLedger.objects.values("crop_id").annotate(
            latest_id=Max("id")
        )

        latest_entries = (
            InventoryLedger.objects.filter(
                id__in=[item["latest_id"] for item in crops_with_inventory]
            )
            .select_related("crop")
            .order_by("crop__name")
        )

        inventory_items = []

        for entry in latest_entries:
            if entry.running_balance <= 0:
                continue

            crop = entry.crop
            balance = entry.running_balance
            expiry = entry.expiry_date

            # Calculate average weekly draw rate (last 4 weeks)
            four_weeks_ago = today - timedelta(weeks=4)

            recent_draws = InventoryLedger.objects.filter(
                crop=crop,
                event_type="sale_out",
                event_date__gte=four_weeks_ago,
                event_date__lte=today,
            ).aggregate(
                total_drawn=Sum("quantity")  # negative values
            )[
                "total_drawn"
            ] or Decimal(
                "0"
            )

            # quantity is negative for sale_out, so negate
            weekly_draw = abs(recent_draws) / 4 if recent_draws else Decimal("0")

            # Weeks of supply remaining
            weeks_remaining = None
            runout_date = None
            if weekly_draw > 0:
                weeks_remaining = int(balance / weekly_draw)
                runout_date = today + timedelta(weeks=weeks_remaining)

            # Expiry warning
            weeks_to_expiry = None
            if expiry:
                weeks_to_expiry = (expiry - today).days // 7

            # Will it expire before being sold?
            excess_at_expiry = None
            if weeks_to_expiry is not None and weekly_draw > 0:
                sold_by_expiry = weekly_draw * weeks_to_expiry
                excess_at_expiry = max(Decimal("0"), balance - sold_by_expiry)

            # Recent transactions
            recent_txns = InventoryLedger.objects.filter(
                crop=crop,
                event_date__gte=four_weeks_ago,
            ).order_by("-event_date", "-created_at")[:10]

            status = "good"
            if weeks_to_expiry is not None and weeks_to_expiry < 3:
                status = "critical"
            elif excess_at_expiry and excess_at_expiry > 0:
                status = "warning"
            elif weeks_remaining is not None and weeks_remaining < 4:
                status = "low"

            inventory_items.append(
                {
                    "crop": crop,
                    "balance": balance,
                    "unit": crop.harvest_unit,
                    "expiry_date": expiry,
                    "weeks_to_expiry": weeks_to_expiry,
                    "weekly_draw": weekly_draw,
                    "weeks_remaining": weeks_remaining,
                    "runout_date": runout_date,
                    "excess_at_expiry": excess_at_expiry,
                    "recent_txns": recent_txns,
                    "status": status,
                    "storage_location": entry.storage_location,
                }
            )

        # Sort: critical first, then warning, then by weeks remaining
        status_order = {"critical": 0, "warning": 1, "low": 2, "good": 3}
        inventory_items.sort(
            key=lambda i: (
                status_order.get(i["status"], 99),
                i["weeks_remaining"] or 999,
            )
        )

        total_value = Decimal("0")
        for item in inventory_items:
            fmt = (
                CropSalesFormat.objects.filter(crop=item["crop"], is_active=True)
                .order_by("-sale_price")
                .first()
            )
            if fmt:
                units = item["balance"] / fmt.harvest_qty_per_sale_unit
                item["estimated_value"] = units * (fmt.sale_price or Decimal("0"))
                total_value += item["estimated_value"]
            else:
                item["estimated_value"] = None

        fresh_items = [i for i in inventory_items if i["crop"].fresh_or_storage != "storage"]
        storage_items = [i for i in inventory_items if i["crop"].fresh_or_storage == "storage"]

        def _rollup(section):
            return {
                "total_items": len(section),
                "total_value": sum(
                    (i["estimated_value"] or Decimal("0")) for i in section
                ),
                "critical_count": sum(1 for i in section if i["status"] == "critical"),
                "warning_count": sum(1 for i in section if i["status"] == "warning"),
            }

        ctx.update(
            {
                "items": inventory_items,
                "fresh_items": fresh_items,
                "storage_items": storage_items,
                "fresh_rollup": _rollup(fresh_items),
                "storage_rollup": _rollup(storage_items),
                "total_items": len(inventory_items),
                "total_value": total_value,
                "critical_count": sum(1 for i in inventory_items if i["status"] == "critical"),
                "warning_count": sum(1 for i in inventory_items if i["status"] == "warning"),
            }
        )
        return ctx


class InventoryTransactionView(FormView):
    """Record inventory events: sale_out, waste_out, etc."""

    template_name = "operations/inventory_transaction.html"

    class TransactionForm(forms.Form):
        crop = forms.ModelChoiceField(
            queryset=CropInfo.objects.all().order_by("fresh_or_storage", "crop_type", "name")
        )
        event_type = forms.ChoiceField(
            choices=[
                ("sale_out", "Sold / Packed for Market"),
                ("waste_out", "Waste / Spoilage"),
                ("return_in", "Returned from Market"),
                ("adjustment", "Count Adjustment"),
                ("quality_check", "Quality Check (no change)"),
            ]
        )
        quantity = forms.DecimalField(
            max_digits=10,
            decimal_places=2,
            help_text="Positive number. System applies sign based on event type.",
        )
        notes = forms.CharField(
            widget=forms.Textarea(attrs={"rows": 2}),
            required=False,
        )

    form_class = TransactionForm

    def form_valid(self, form):
        if not self.request.user.is_authenticated:
            return redirect(f"/admin/login/?next={self.request.path}")
        if not self.request.user.is_staff:
            return HttpResponse(status=403)
        crop = form.cleaned_data["crop"]
        event_type = form.cleaned_data["event_type"]
        raw_qty = form.cleaned_data["quantity"]
        notes = form.cleaned_data["notes"]

        # Apply sign convention
        if event_type in ("sale_out", "waste_out"):
            quantity = -abs(raw_qty)
        elif event_type == "quality_check":
            quantity = Decimal("0")
        else:
            quantity = abs(raw_qty)

        # Get the latest entry for running balance
        last = (
            InventoryLedger.objects.filter(crop=crop).order_by("-event_date", "-created_at").first()
        )

        prev_balance = last.running_balance if last else Decimal("0")
        new_balance = prev_balance + quantity

        if new_balance < 0:
            messages.warning(
                self.request,
                f"Warning: balance would go negative ({new_balance}). "
                f"Recording anyway — may need adjustment.",
            )

        InventoryLedger.objects.create(
            crop=crop,
            event_date=date.today(),
            event_type=event_type,
            quantity=quantity,
            running_balance=new_balance,
            expiry_date=last.expiry_date if last else None,
            storage_location=last.storage_location if last else "",
            notes=notes,
        )

        messages.success(
            self.request,
            f"Recorded: {event_type} {abs(raw_qty)} {crop.harvest_unit} "
            f"of {crop.name}. Balance: {new_balance}",
        )

        return redirect("operations:inventory")

    def form_invalid(self, form):
        # Keep malformed payload handling deterministic for gate tests even if
        # template wiring is incomplete in certain environments.
        return HttpResponse(status=400)


class FieldWalkView(OperationsPlanningYearMixin, TemplateView):
    """Walk-route ordered checklist of all active plantings (unified week-ops)."""

    template_name = "operations/field_walk.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_num = max(1, min(52, int(week_num)))
        today = date.today()
        wctx = week_ops_service.week_context(
            self.year_obj, week_num, today=today, mode="field_walk"
        )
        for g in wctx["blocks"]:
            g["kind"] = "walk"
        total_plantings = sum(len(g["plantings"]) for g in wctx["blocks"])
        ctx.update(_weekops_header_context("walk", week_num, self.year_obj, wctx))
        ctx.update(
            {
                "weekops": wctx,
                "today": today,
                "blocks": wctx["blocks"],
                "total_plantings": total_plantings,
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)

        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_num = max(1, min(52, int(week_num)))
        today = date.today()
        year_obj = self.year_obj
        redirect_name = "operations:weekops_walk"
        redirect_kw = {"week": week_num}

        no_change_block = request.POST.get("no_change_block")
        if no_change_block:
            try:
                block_id = int(no_change_block)
            except (TypeError, ValueError):
                block_id = None
            if block_id is not None:
                qs = Planting.objects.filter(
                    planning_year=year_obj,
                    block_id=block_id,
                    status__in=["planted", "growing", "harvesting"],
                )
                n = 0
                for planting in qs:
                    FieldWalkNote.objects.create(
                        planting=planting,
                        walk_date=today,
                        condition="good",
                        yield_adjust_pct=100,
                        notes="",
                    )
                    if planting.status == PlantingStatus.PLANTED:
                        planting.status = PlantingStatus.GROWING
                        planting.save(update_fields=["status"])
                    n += 1
                messages.success(
                    request,
                    f"Recorded walk (no change) for {n} planting(s) in this block.",
                )
                return redirect(redirect_name, **redirect_kw)

        notes_created = 0
        total_adj_weeks = 0
        for key, value in request.POST.items():
            if key.startswith("condition_") and value:
                planting_id = key.replace("condition_", "")

                try:
                    planting = Planting.objects.get(
                        id=planting_id,
                        planning_year=year_obj,
                    )
                except Planting.DoesNotExist:
                    continue

                condition = value
                notes_text = request.POST.get(f"notes_{planting_id}", "")
                yield_pct = request.POST.get(f"yield_{planting_id}", "100")
                adjusted_harvest = request.POST.get(f"adj_harvest_{planting_id}", "")

                try:
                    yield_pct = int(yield_pct)
                except ValueError:
                    yield_pct = 100

                fw = FieldWalkNote.objects.create(
                    planting=planting,
                    walk_date=today,
                    condition=condition,
                    yield_adjust_pct=yield_pct,
                    notes=notes_text,
                )

                n_adj = apply_yield_adjustment_to_future_harvests(planting, yield_pct)
                total_adj_weeks += n_adj

                if adjusted_harvest:
                    try:
                        adj_week = int(adjusted_harvest)
                        fw.adjusted_first_harvest_date = Week(year_obj.year, adj_week).monday()
                        fw.save()
                    except (ValueError, TypeError):
                        pass

                if condition == "failed":
                    planting.status = "failed"
                    planting.notes += f"\nFailed: {today} — {notes_text}"
                    planting.save()
                elif planting.status == PlantingStatus.PLANTED:
                    planting.status = PlantingStatus.GROWING
                    planting.save(update_fields=["status"])

                notes_created += 1

        if total_adj_weeks:
            messages.info(
                request,
                f"Adjusted {total_adj_weeks} planned harvest week(s) total from yield changes.",
            )
        messages.success(
            request,
            f"Field walk complete. Recorded {notes_created} observation(s).",
        )
        return redirect(redirect_name, **redirect_kw)


class PlantingRecordView(OperationsPlanningYearMixin, TemplateView):
    """Rich form for actual planting / plan deviation (operations entry point)."""

    template_name = "operations/planting_record.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        planting = get_object_or_404(
            Planting.objects.select_related(
                "crop", "crop_season", "block", "planning_year", "variety_obj"
            ),
            pk=kwargs["pk"],
            planning_year=self.year_obj,
        )
        variety_display = ""
        if planting.variety_obj_id:
            variety_display = planting.variety_obj.name
        elif planting.variety:
            variety_display = planting.variety
        field_notes = list(planting.field_walk_notes.order_by("-walk_date", "-id")[:25])
        today = date.today()
        ctx.update(
            {
                "year": self.year_obj,
                "planting": planting,
                "variety_display": variety_display,
                "field_walk_notes": field_notes,
                "planting_status_choices": PlantingStatus.choices,
                "today": today,
                "planting_display_id": format_planting_display_id(planting.pk),
                "schedule_chip_class": planting_schedule_chip_css_class(
                    planting.planned_plant_date, planting.actual_plant_date, today
                ),
            }
        )
        return ctx

    def post(self, request, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        planting = get_object_or_404(
            Planting.objects.select_related("crop", "crop_season", "block"),
            pk=kwargs["pk"],
            planning_year=self.year_obj,
        )

        status = (request.POST.get("status") or planting.status).strip()
        notes_add = (request.POST.get("notes", "") or "").strip()
        apd = request.POST.get("actual_plant_date", "").strip()
        abf = request.POST.get("actual_bedfeet", "").strip()

        valid = {c[0] for c in PlantingStatus.choices}
        if status in valid:
            planting.status = status

        if apd:
            try:
                planting.actual_plant_date = date.fromisoformat(apd)
            except ValueError:
                messages.warning(request, "Invalid actual plant date; left unchanged.")
        if abf:
            try:
                planting.actual_bedfeet = int(abf)
            except ValueError:
                messages.warning(request, "Invalid actual bedfeet; left unchanged.")

        if notes_add:
            planting.notes = (planting.notes + "\n" + notes_add).strip()

        planting.save()
        messages.success(request, "Planting record updated.")
        return redirect("operations:planting_record", pk=planting.pk)


class HarvestNeedsView(OperationsPlanningYearMixin, TemplateView):
    """Harvest events scheduled for the selected week (unified week-ops surface)."""

    template_name = "operations/harvest_needs.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_num = max(1, min(52, int(week_num)))
        today = date.today()
        wctx = week_ops_service.week_context(
            self.year_obj, week_num, today=today, mode="harvest_needs"
        )
        harvest_blocks = []
        for group in wctx["blocks"]:
            rows_open = []
            rows_done = []
            for prow in group["plantings"]:
                p = prow["planting"]
                for he in prow["harvest_events_this_week"]:
                    drow = wctx["sales_demand_by_crop"].get(p.crop_id)
                    sales_str = ""
                    if drow and drow.get("qty"):
                        u = (drow.get("sale_unit") or "").strip()
                        sales_str = f"{drow['qty']} {u}".strip()
                    row = {
                        "event": he,
                        "planting": p,
                        "prow": prow,
                        "variety_label": prow["variety_label"],
                        "target_bins": week_ops_service.target_bins_for_event(he),
                        "sales_committed_str": sales_str,
                    }
                    if he.actual_quantity is None:
                        rows_open.append(row)
                    else:
                        rows_done.append(row)
            harvest_blocks.append(
                {
                    "block": group["block"],
                    "kind": "harvest",
                    "rows_open": rows_open,
                    "rows_done": rows_done,
                }
            )

        ctx.update(_weekops_header_context("needs", week_num, self.year_obj, wctx))
        week_rollup_list = sorted(
            wctx["week_rollup_by_crop"].values(),
            key=lambda r: r["crop"].name.lower(),
        )
        ctx.update(
            {
                "weekops": wctx,
                "week_rollup_by_crop": wctx["week_rollup_by_crop"],
                "week_rollup_list": week_rollup_list,
                "week_rollup_by_channel": wctx["week_rollup_by_channel"],
                "week_rollup_by_sales_category": wctx["week_rollup_by_sales_category"],
                "sales_demand_by_crop": wctx["sales_demand_by_crop"],
                "harvest_blocks": harvest_blocks,
            }
        )
        return ctx


class FieldWalkPrintView(FieldWalkView):
    """Printable walk-route list for a week."""

    template_name = "operations/field_walk_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["field_week_print_kind"] = "walk_print"
        return ctx


class HarvestNeedsPrintView(HarvestNeedsView):
    """Printable harvest-needs checklist for a week."""

    template_name = "operations/harvest_needs_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["field_week_print_kind"] = "needs_print"
        return ctx


class FieldWalkCurrentRedirect(RedirectView):
    permanent = False

    def get_redirect_url(self, *args, **kwargs):
        w = max(1, min(52, date.today().isocalendar()[1]))
        return reverse("operations:weekops_walk", kwargs={"week": w})


class HarvestNeedsCurrentRedirect(RedirectView):
    permanent = False

    def get_redirect_url(self, *args, **kwargs):
        w = max(1, min(52, date.today().isocalendar()[1]))
        return reverse("operations:weekops_needs", kwargs={"week": w})


class HarvestEntryCurrentRedirect(RedirectView):
    permanent = False

    def get_redirect_url(self, *args, **kwargs):
        w = max(1, min(52, date.today().isocalendar()[1]))
        return reverse("operations:weekops_record", kwargs={"week": w})


class MissingPlantingsView(OperationsPlanningYearMixin, TemplateView):
    """Plantings past due plant date but still in planned status."""

    template_name = "operations/missing_plantings.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.year_obj
        today = date.today()
        qs = (
            Planting.objects.filter(
                planning_year=year_obj,
                planned_plant_date__lte=today,
                status="planned",
            )
            .select_related("crop", "block", "variety_obj")
            .order_by("planned_plant_date", "block__walk_route_order", "block__name", "bed_start")
        )
        ctx.update({"year": year_obj, "today": today, "plantings": qs})
        return ctx


class PrintablePlantingListView(OperationsPlanningYearMixin, TemplateView):
    """Printable planting list with week range."""

    template_name = "operations/planting_list_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.year_obj
        req = self.request.GET
        try:
            wk_from = int(req.get("from_week", 1))
            wk_to = int(req.get("to_week", 52))
        except ValueError:
            wk_from, wk_to = 1, 52
        wk_from = max(1, min(52, wk_from))
        wk_to = max(1, min(52, wk_to))
        if wk_to < wk_from:
            wk_from, wk_to = wk_to, wk_from

        year = year_obj.year
        start_d = Week(year, wk_from).monday()
        end_d = Week(year, wk_to).monday() + timedelta(days=6)

        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
                planned_plant_date__gte=start_d,
                planned_plant_date__lte=end_d,
            )
            .exclude(status__in=["skipped", "failed", "revised"])
            .select_related("crop", "crop_season", "block", "variety_obj")
            .order_by("block__walk_route_order", "block__name", "bed_start", "planned_plant_date")
        )
        rows = []
        today = date.today()
        for p in plantings:
            v = p.variety_obj.name if p.variety_obj_id else (p.variety or "—")
            rows.append(
                {
                    "planting": p,
                    "variety": v,
                    "harvest_wk": f"{p.planned_first_harvest_date.isocalendar()[1]}-{p.planned_last_harvest_date.isocalendar()[1]}",
                    "planting_display_id": format_planting_display_id(p.pk),
                    "schedule_chip_class": planting_schedule_chip_css_class(
                        p.planned_plant_date, p.actual_plant_date, today
                    ),
                }
            )
        ctx.update(
            {
                "year": year_obj,
                "wk_from": wk_from,
                "wk_to": wk_to,
                "rows": rows,
            }
        )
        return ctx


class PrintableSeedingTodoView(OperationsPlanningYearMixin, TemplateView):
    """Printable direct-seed + greenhouse seed events for a week range."""

    template_name = "operations/seeding_todo_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year_obj = self.year_obj
        req = self.request.GET
        try:
            wk_from = int(req.get("from_week", date.today().isocalendar()[1]))
            wk_to = int(req.get("to_week", wk_from + 2))
        except ValueError:
            wk_from = date.today().isocalendar()[1]
            wk_to = wk_from + 2
        year = year_obj.year
        start_d = Week(year, max(1, min(52, wk_from))).monday()
        end_d = Week(year, max(1, min(52, wk_to))).monday() + timedelta(days=6)

        nursery_rows = []
        today = date.today()
        for ev in (
            NurseryEvent.objects.filter(
                planting__planning_year=year_obj,
                event_type="seed",
                planned_date__gte=start_d,
                planned_date__lte=end_d,
            )
            .exclude(planting__status__in=["skipped", "failed", "revised"])
            .select_related(
                "planting",
                "planting__crop",
                "planting__crop_season",
                "planting__block",
                "planting__variety_obj",
            )
            .order_by("planned_date", "planting__block__walk_route_order")
        ):
            pl = ev.planting
            cs = pl.crop_season
            vl = week_ops_service.variety_label(pl)
            nursery_rows.append(
                {
                    "kind": "greenhouse_seed",
                    "date": ev.planned_date,
                    "crop": pl.crop.name,
                    "block": pl.block.name,
                    "beds": f"b{pl.bed_start}-{pl.bed_end}",
                    "seeder": cs.seeder_settings if cs else "",
                    "ds_rate": cs.ds_seed_rate if cs else None,
                    "notes": ev.notes or "",
                    "event": ev,
                    "planting": pl,
                    "variety_display": vl,
                    "field_week": pl.planned_plant_date.isocalendar()[1],
                    "planting_display_id": format_planting_display_id(pl.pk),
                    "schedule_chip_class": planting_schedule_chip_css_class(
                        pl.planned_plant_date, pl.actual_plant_date, today
                    ),
                }
            )

        field_direct = []
        for p in (
            Planting.objects.filter(
                planning_year=year_obj,
                crop__nursery_weeks=0,
                planned_plant_date__gte=start_d,
                planned_plant_date__lte=end_d,
            )
            .exclude(status__in=["skipped", "failed", "revised"])
            .select_related("crop", "crop_season", "block")
            .order_by("planned_plant_date", "block__walk_route_order")
        ):
            cs = p.crop_season
            field_direct.append(
                {
                    "kind": "field_direct_seed",
                    "date": p.planned_plant_date,
                    "crop": p.crop.name,
                    "block": p.block.name,
                    "beds": f"b{p.bed_start}-{p.bed_end}",
                    "seeder": cs.seeder_settings if cs else "",
                    "ds_rate": cs.ds_seed_rate if cs else None,
                    "notes": p.notes or "",
                    "planting_display_id": format_planting_display_id(p.pk),
                    "schedule_chip_class": planting_schedule_chip_css_class(
                        p.planned_plant_date, p.actual_plant_date, today
                    ),
                }
            )

        ctx.update(
            {
                "year": year_obj,
                "wk_from": wk_from,
                "wk_to": wk_to,
                "nursery_rows": nursery_rows,
                "field_direct": field_direct,
            }
        )
        return ctx


class PackPrepView(OperationsPlanningYearMixin, TemplateView):
    """Packing checklist: plan vs packed vs cooler balance for a channel and date."""

    template_name = "operations/pack_prep.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        channel_id = self.request.GET.get("channel")
        pdate_raw = self.request.GET.get("pack_date")
        channel = (
            get_object_or_404(SalesChannel, pk=channel_id)
            if channel_id
            else SalesChannel.objects.order_by("allocation_priority", "id").first()
        )
        pack_date = date.today()
        if pdate_raw:
            try:
                pack_date = date.fromisoformat(pdate_raw)
            except ValueError:
                pass

        rows = []
        if channel:
            plan_rows = SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
                channel=channel,
                sale_date=pack_date,
            ).select_related("product", "product__crop")

            for pl in plan_rows:
                if not pl.product_id:
                    continue
                planned_qty = pl.planned_quantity or Decimal("0")
                packed = (
                    PackAllocation.objects.filter(
                        channel=channel,
                        product_id=pl.product_id,
                        pack_date=pack_date,
                    ).aggregate(total=Sum("quantity"))["total"]
                    or Decimal("0")
                )
                crop = pl.product.crop
                bal = (
                    InventoryLedger.objects.filter(crop=crop)
                    .order_by("-event_date", "-created_at", "-id")
                    .first()
                )
                on_hand = bal.running_balance if bal else Decimal("0")
                rows.append(
                    {
                        "product": pl.product,
                        "planned_qty": planned_qty,
                        "packed_qty": packed,
                        "shortfall": max(Decimal("0"), planned_qty - packed),
                        "crop_balance": on_hand,
                    }
                )

        ctx.update(
            {
                "year": self.year_obj,
                "channel": channel,
                "channels": SalesChannel.objects.order_by("allocation_priority", "name"),
                "pack_date": pack_date,
                "rows": rows,
            }
        )
        return ctx


class PackBatchRecordForm(forms.Form):
    channel = forms.ModelChoiceField(
        queryset=SalesChannel.objects.all().order_by("allocation_priority", "name")
    )
    product = forms.ModelChoiceField(
        queryset=CropSalesFormat.objects.filter(is_active=True)
        .select_related("crop")
        .order_by("crop__name", "product_name")
    )
    pack_date = forms.DateField()
    packed_quantity = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    packed_unit = forms.CharField(max_length=20, required=False, help_text="Defaults to product sale unit.")
    post_consumption = forms.BooleanField(
        required=False,
        initial=True,
        help_text="For mix products, post crop drawdown to inventory ledger after save.",
    )


class PackBatchRecordView(OperationsPlanningYearMixin, FormView):
    template_name = "operations/pack_batch_record.html"
    form_class = PackBatchRecordForm

    def form_valid(self, form):
        if not self.request.user.is_authenticated:
            return redirect(f"/admin/login/?next={self.request.path}")
        if not self.request.user.is_staff:
            return HttpResponse(status=403)

        channel = form.cleaned_data["channel"]
        product = form.cleaned_data["product"]
        pack_date = form.cleaned_data["pack_date"]
        pq = form.cleaned_data["packed_quantity"]
        pu = (form.cleaned_data["packed_unit"] or "").strip() or product.sale_unit
        post_consume = form.cleaned_data["post_consumption"]

        batch = PackBatch.objects.create(
            product=product,
            packed_quantity=pq,
            packed_unit=pu,
            pack_date=pack_date,
            notes=f"Recorded via pack batch form ({channel.name})",
        )

        recipe = (
            ProductRecipe.objects.filter(product=product, is_active=True)
            .order_by("-planning_year__year", "-id")
            .first()
        )
        if recipe:
            out_u = (recipe.output_unit or product.sale_unit or "").strip().casefold()
            if out_u != pu.strip().casefold():
                batch.delete()
                form.add_error(
                    None,
                    "Packed unit must match the active recipe output unit "
                    f"({recipe.output_unit or product.sale_unit}).",
                )
                return self.form_invalid(form)
            factor = pq
            for rc in recipe.components.select_related("source_crop", "source_product").order_by(
                "sort_order", "id"
            ):
                PackBatchComponent.objects.create(
                    pack_batch=batch,
                    source_crop=rc.source_crop,
                    source_product=rc.source_product,
                    consumed_quantity=rc.component_quantity * factor,
                    consumed_unit=rc.component_unit,
                    component_percent=rc.component_percent,
                )
            if post_consume:
                try:
                    batch.post_component_consumption()
                except ValidationError as e:
                    batch.delete()
                    form.add_error(None, e.messages[0] if e.messages else str(e))
                    return self.form_invalid(form)
        PackAllocation.objects.create(
            channel=channel,
            product=product,
            pack_date=pack_date,
            quantity=pq,
            pack_batch=batch,
        )
        messages.success(
            self.request,
            f"Pack batch recorded: {product.product_name} × {pq} {pu} for {channel.name} on {pack_date}.",
        )
        return redirect(
            f"{reverse('operations:pack_prep')}?channel={channel.id}&pack_date={pack_date.isoformat()}"
        )

    def get_initial(self):
        initial = super().get_initial()
        initial.setdefault("pack_date", date.today())
        return initial
