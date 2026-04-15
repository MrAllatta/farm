# operations/views.py

from django.views.generic import TemplateView, FormView
from django.shortcuts import redirect
from django.contrib import messages
from django.db.models import Q, Max, Subquery, OuterRef, Sum
from django.http import HttpResponse
from datetime import date, timedelta
from isoweek import Week
from django import forms
from reference.models import CropInfo, CropSalesFormat
from operations.models import InventoryLedger
from planning.models import Planting, HarvestEvent, PlanningYear
from decimal import Decimal


class InventoryHarvestInView(TemplateView):
    """Add harvest to inventory"""

    template_name = "operations/harvest_entry.html"


class FieldWalkNoteView(TemplateView):
    """Field walk notes"""

    template_name = "operations/harvest_entry.html"


class PlantingHarvestEntryView(TemplateView):
    """Planting Harvest Entry"""

    template_name = "operations/harvest_entry.html"


class WeeklyHarvestEntryView(TemplateView):
    """Batch harvest entry for a given week.

    Shows all plantings expected to produce this week,
    with bin-entry fields for actual quantities.
    """

    template_name = "operations/harvest_entry.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = PlanningYear.objects.filter(status="active").first()
        week_num = kwargs.get("week", date.today().isocalendar()[1])
        year = year_obj.year if year_obj else date.today().year

        week_monday = Week(year, week_num).monday()
        week_sunday = week_monday + timedelta(days=6)

        # Get all harvest events for this week
        harvest_events = (
            HarvestEvent.objects.filter(
                planting__planning_year=year_obj,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .select_related("planting", "planting__crop", "planting__block")
            .order_by(
                "planting__block__walk_route_order",
                "planting__block__name",
                "planting__bed_start",
            )
        )

        # Group by block for the harvest route
        blocks = {}
        for he in harvest_events:
            block_name = he.planting.block.name
            if block_name not in blocks:
                blocks[block_name] = []

            crop = he.planting.crop
            blocks[block_name].append(
                {
                    "event": he,
                    "planting": he.planting,
                    "crop_name": crop.name,
                    "block": he.planting.block.name,
                    "beds": f"{he.planting.bed_start}-{he.planting.bed_end}",
                    "target_qty": he.planned_quantity,
                    "units": he.planned_units,
                    "bin_type": crop.harvest_bin,
                    "units_per_bin": crop.units_per_bin,
                    "target_bins": (
                        float(he.planned_quantity) / crop.units_per_bin
                        if crop.units_per_bin
                        else None
                    ),
                    "has_actual": he.actual_quantity is not None,
                    "actual_qty": he.actual_quantity,
                    "actual_bins": he.actual_bins,
                }
            )

        # Summary stats
        total_items = harvest_events.count()
        recorded = harvest_events.filter(actual_quantity__isnull=False).count()

        total_bins = sum(
            item["target_bins"] or 0 for block_items in blocks.values() for item in block_items
        )

        ctx.update(
            {
                "year": year_obj,
                "week_num": week_num,
                "week_monday": week_monday,
                "blocks": blocks,
                "total_items": total_items,
                "recorded": recorded,
                "total_bins": total_bins,
                "prev_week": week_num - 1 if week_num > 1 else 52,
                "next_week": week_num + 1 if week_num < 52 else 1,
            }
        )
        return ctx

    def post(self, request, **kwargs):
        """Handle batch harvest entry submission."""
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        year_obj = PlanningYear.objects.filter(status="active").first()

        updated = 0
        for key, value in request.POST.items():
            if key.startswith("bins_") and value:
                event_id = key.replace("bins_", "")
                try:
                    he = HarvestEvent.objects.get(
                        id=event_id,
                        planting__planning_year=year_obj,
                    )
                    bin_count = float(value)
                    he.record_bins(bin_count)

                    # Also capture notes if provided
                    notes_key = f"notes_{event_id}"
                    if notes_key in request.POST:
                        he.notes = request.POST[notes_key]
                        he.save()

                    updated += 1
                except (HarvestEvent.DoesNotExist, ValueError):
                    continue

        messages.success(request, f"Recorded {updated} harvest entries.")

        # Update planting status if first harvest recorded
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

        return redirect("operations:harvest_entry_week", week=kwargs.get("week"))


class InventoryDashboardView(TemplateView):
    """Storage crop inventory with drawdown projections."""

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
                item["estimated_value"] = units * fmt.sale_price
                total_value += item["estimated_value"]
            else:
                item["estimated_value"] = None

        ctx.update(
            {
                "items": inventory_items,
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
        crop = forms.ModelChoiceField(queryset=CropInfo.objects.filter(fresh_or_storage="storage"))
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


class FieldWalkView(TemplateView):
    """Walk-route ordered checklist of all active plantings."""

    template_name = "operations/field_walk.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = PlanningYear.objects.filter(status="active").first()
        today = date.today()

        # Active plantings ordered by walk route
        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
                status__in=["planted", "growing", "harvesting"],
            )
            .select_related("crop", "crop_season", "block")
            .order_by(
                "block__walk_route_order",
                "block__name",
                "bed_start",
            )
        )

        # Get most recent field walk note for each planting

        latest_notes = {}
        for p in plantings:
            note = p.field_walk_notes.order_by("-walk_date").first()
            latest_notes[p.id] = note

        # Group by block
        blocks = {}
        for p in plantings:
            block_name = p.block.name
            if block_name not in blocks:
                blocks[block_name] = {
                    "block": p.block,
                    "plantings": [],
                }

            # Calculate expected stage
            plant_date = p.actual_plant_date or p.planned_plant_date
            days_since_plant = (today - plant_date).days if plant_date else 0
            weeks_since_plant = days_since_plant // 7

            dtm = p.crop_season.dtm_days
            harvest_start = p.actual_first_harvest_date or p.planned_first_harvest_date

            if today >= harvest_start:
                weeks_harvesting = (today - harvest_start).days // 7
                expected_stage = (
                    f"Harvesting (week {weeks_harvesting + 1} of {p.crop_season.harvest_weeks})"
                )
            elif days_since_plant > dtm * 0.75:
                expected_stage = f"Approaching harvest ({weeks_since_plant}wk, DTM {dtm}d)"
            elif days_since_plant > dtm * 0.5:
                expected_stage = f"Mid-growth ({weeks_since_plant}wk)"
            else:
                expected_stage = f"Establishing ({weeks_since_plant}wk)"

            last_note = latest_notes.get(p.id)

            blocks[block_name]["plantings"].append(
                {
                    "planting": p,
                    "crop_name": p.crop.name,
                    "variety": p.variety,
                    "beds": f"{p.bed_start}-{p.bed_end}",
                    "bedfeet": p.planned_bedfeet,
                    "expected_stage": expected_stage,
                    "expected_harvest": harvest_start,
                    "last_note": last_note,
                    "days_since_last_note": (
                        (today - last_note.walk_date).days if last_note else None
                    ),
                }
            )

        ctx.update(
            {
                "year": year_obj,
                "today": today,
                "blocks": blocks,
                "total_plantings": plantings.count(),
            }
        )
        return ctx

    def post(self, request, **kwargs):
        """Handle field walk note submissions."""
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        year_obj = PlanningYear.objects.filter(status="active").first()
        today = date.today()

        notes_created = 0

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

                # Parse adjusted harvest date if provided
                if adjusted_harvest:
                    try:
                        adj_week = int(adjusted_harvest)
                        fw.adjusted_first_harvest_date = Week(year_obj.year, adj_week).monday()
                        fw.save()
                    except (ValueError, TypeError):
                        pass

                # Update planting status if marked as failed
                if condition == "failed":
                    planting.status = "failed"
                    planting.notes += f"\nFailed: {today} — {notes_text}"
                    planting.save()

                notes_created += 1

        messages.success(request, f"Field walk complete. Recorded {notes_created} observations.")
        return redirect("operations:field_walk_current")


class SeedOrderReportView(TemplateView):
    """Calculate seed needs from all planned plantings."""

    template_name = "reports/seed_order.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = PlanningYear.objects.filter(status__in=["planning", "active"]).first()

        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
            )
            .exclude(status="skipped")
            .select_related("crop", "crop_season", "block")
        )

        overplant = float(year_obj.overplant_factor)

        # Aggregate by crop
        crop_needs = {}  # crop_id → {crop, total_bedfeet, seed_calc}

        for p in plantings:
            crop = p.crop
            cs = p.crop_season
            crop_id = crop.id

            if crop_id not in crop_needs:
                crop_needs[crop_id] = {
                    "crop": crop,
                    "total_bedfeet": 0,
                    "plantings": [],
                }

            crop_needs[crop_id]["total_bedfeet"] += p.planned_bedfeet
            crop_needs[crop_id]["plantings"].append(p)

        # Calculate seed quantities
        seed_orders = []

        for crop_id, data in crop_needs.items():
            crop = data["crop"]
            cs = data["plantings"][0].crop_season  # use first planting's profile
            total_bf = data["total_bedfeet"]

            result = self._calculate_seeds(crop, cs, total_bf, overplant)
            result["crop"] = crop
            result["total_bedfeet"] = total_bf
            result["num_plantings"] = len(data["plantings"])
            seed_orders.append(result)

        # Sort: direct seeded first, then transplanted, then vegetative
        seed_orders.sort(
            key=lambda x: (
                0 if x["method"] == "direct_seed" else 1 if x["method"] == "transplant" else 2,
                x["crop"].name,
            )
        )

        ctx.update(
            {
                "year": year_obj,
                "overplant_pct": int((overplant - 1) * 100),
                "seed_orders": seed_orders,
                "direct_seeded": [s for s in seed_orders if s["method"] == "direct_seed"],
                "transplanted": [s for s in seed_orders if s["method"] == "transplant"],
                "vegetative": [s for s in seed_orders if s["method"] == "vegetative"],
            }
        )
        return ctx

    def _calculate_seeds(self, crop, crop_season, total_bedfeet, overplant):
        """Three calculation paths depending on propagation type."""

        if crop.propagation_type != "seed":
            return self._calc_vegetative(crop, crop_season, total_bedfeet, overplant)

        if crop_season.ds_seed_rate:
            return self._calc_direct_seed(crop, crop_season, total_bedfeet, overplant)

        if crop_season.tp_inrow_spacing:
            return self._calc_transplant(crop, crop_season, total_bedfeet, overplant)

        return {
            "method": "unknown",
            "seeds_needed": 0,
            "ounces_needed": 0,
            "order_rounded": "?",
            "calculation": "Missing seed rate and spacing data",
        }

    def _calc_direct_seed(self, crop, cs, total_bf, overplant):
        rows = cs.rows_per_bed or 1
        rate = cs.ds_seed_rate  # seeds per rowfoot

        seeds = total_bf * rows * rate * overplant

        ounces = None
        order = None
        if crop.seeds_per_ounce and crop.seeds_per_ounce > 0:
            ounces = seeds / float(crop.seeds_per_ounce)
            order = self._round_order(ounces)

        return {
            "method": "direct_seed",
            "seeds_needed": int(seeds),
            "ounces_needed": ounces,
            "order_rounded": order,
            "calculation": (
                f"{total_bf}bf × {rows}rows × {rate}seeds/rf "
                f"× {overplant} overplant = {int(seeds)} seeds"
            ),
        }

    def _calc_transplant(self, crop, cs, total_bf, overplant):
        rows = cs.rows_per_bed or 1
        spacing = float(cs.tp_inrow_spacing)

        plants = total_bf * rows / spacing * overplant

        seeds_per_cell = crop.seeds_per_cell or 1
        thinned = crop.thinned_plants or 0

        if thinned > 0 and seeds_per_cell > 1:
            # Multi-seed, thin to one: cells needed = plants needed
            cells = plants
        else:
            cells = plants

        seeds = cells * seeds_per_cell

        # Tray count
        trays = None
        if crop.seeded_tray_size and crop.seeded_tray_size > 1:
            trays = math.ceil(cells / crop.seeded_tray_size)

        ounces = None
        order = None
        if crop.seeds_per_ounce and crop.seeds_per_ounce > 0:
            ounces = seeds / float(crop.seeds_per_ounce)
            order = self._round_order(ounces)

        return {
            "method": "transplant",
            "plants_needed": int(plants),
            "cells_needed": int(cells),
            "seeds_needed": int(seeds),
            "trays_needed": trays,
            "tray_size": crop.seeded_tray_size,
            "ounces_needed": ounces,
            "order_rounded": order,
            "calculation": (
                f"{total_bf}bf × {rows}rows ÷ {spacing}ft spacing "
                f"× {overplant} = {int(plants)} plants, "
                f"{int(seeds)} seeds ({seeds_per_cell}/cell)"
            ),
        }

    def _calc_vegetative(self, crop, cs, total_bf, overplant):
        rows = cs.rows_per_bed or 1
        spacing = float(cs.tp_inrow_spacing) if cs.tp_inrow_spacing else 1

        pieces = total_bf * rows / spacing * overplant

        # Garlic: ~60 cloves per pound
        # Potato: ~2 pieces per pound (cut seed potatoes)
        weight_per_piece = {
            "vegetative_clove": 60,  # cloves per lb
            "vegetative_tuber": 2,  # pieces per lb
            "vegetative_slip": None,  # ordered by count
        }

        pcs_per_lb = weight_per_piece.get(crop.propagation_type)
        order_weight = None
        if pcs_per_lb:
            order_weight = f"{math.ceil(pieces / pcs_per_lb)} lb"
        else:
            order_weight = f"{int(pieces)} slips"

        return {
            "method": "vegetative",
            "pieces_needed": int(pieces),
            "seeds_needed": 0,
            "ounces_needed": None,
            "order_rounded": order_weight,
            "calculation": (
                f"{total_bf}bf × {rows}rows ÷ {spacing}ft " f"× {overplant} = {int(pieces)} pieces"
            ),
        }

    def _round_order(self, ounces):
        """Round to common seed packet sizes."""
        if ounces is None:
            return "?"
        if ounces < 0.1:
            return "1 pkt"
        if ounces < 0.25:
            return "1/4 oz"
        if ounces < 0.5:
            return "1/2 oz"
        if ounces < 1:
            return "1 oz"
        if ounces < 4:
            return f"{math.ceil(ounces)} oz"
        # Convert to pounds
        lbs = ounces / 16
        if lbs < 1:
            return f"{math.ceil(ounces)} oz ({lbs:.1f} lb)"
        return f"{math.ceil(lbs)} lb"
