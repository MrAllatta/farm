"""planning.views"""

import math

from django.shortcuts import get_object_or_404, render
from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages
from django.views.generic import TemplateView
from django.db.models import Q, Sum
from datetime import date
from isoweek import Week

from reference.models import Block, BlockType, CropBySeason, CropInfo, CropSalesFormat, SalesChannel
from .models import Planting, HarvestEvent, NurseryEvent, PlantingStatus
from core.planning_year import get_effective_planning_year
from django.views.generic import DetailView, CreateView, UpdateView, View, FormView
from sales.models import SalesEvent

from django.http import HttpResponse
from django import forms
from django.template.loader import render_to_string
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional


class PlanningMatrixView(TemplateView):
    template_name = "planning/matrix.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year_obj = get_effective_planning_year(self.request)

        if not year_obj:
            ctx["no_year"] = True
            return ctx

        year = year_obj.year

        # Current or requested week
        requested_date = kwargs.get("date")
        requested_week = kwargs.get("week")

        if requested_date:
            center_date = date.fromisoformat(requested_date)
        elif requested_week:
            center_date = Week(year, requested_week).monday()
        else:
            center_date = date.today()

        window_start_date = center_date - timedelta(weeks=4)
        window_end_date = center_date + timedelta(weeks=11) # 16 week window

        weeks = []
        current = window_start_date
        while current <= window_end_date:
            weeks.append({
                'date': current,
                'num': current.isocalendar()[1],
                'is_current': current.isocalendar()[1] == date.today().isocalendar()[1]
            })
            current += timedelta(weeks=1)

        # Show 16-week window (scrollable)
        week_start = Week.withdate(window_start_date).monday()
        week_end = Week.withdate(window_end_date).monday()

        # All blocks, grouped by type
        blocks = Block.objects.all().order_by("walk_route_order", "name")
        field_blocks = blocks.filter(block_type=BlockType.FIELD)
        tunnel_blocks = blocks.filter(block_type=BlockType.HIGH_TUNNEL)
        greenhouse_blocks = blocks.filter(block_type=BlockType.GREENHOUSE)

        # Query plantings that overlap the visible window
        plantings = (
            Planting.objects.filter(
                planning_year=year_obj,
                planned_plant_date__lte=window_end_date,
                planned_last_harvest_date__gte=window_start_date,
            )
            .exclude(status="skipped")
            .select_related("crop", "crop_season", "block")
            .order_by("block__name", "bed_start", "planned_plant_date")
        )

        # Build the matrix: block → list of plantings with week positions
        matrix = self._build_matrix(blocks, plantings, weeks, year)

        # Extract week number range for display
        week_nums = [w['num'] for w in weeks]

        ctx.update(
            {
                "year": year_obj,
                "weeks": weeks,
                "week_start": week_nums[0],
                "week_end": week_nums[-1],
                "field_blocks": field_blocks,
                "tunnel_blocks": tunnel_blocks,
                "greenhouse_blocks": greenhouse_blocks,
                "matrix": matrix,
                "plantings": plantings,
                "prev_date": window_start_date - timedelta(weeks=8),
                "next_date": window_end_date + timedelta(days=1),
            }
        )
        return ctx

    def _build_matrix(self, blocks, plantings, weeks, year):
        """Build a dict: block_id → list of planting display objects."""
        matrix = {}

        # Extract week numbers from weeks list
        week_nums = [w['num'] for w in weeks]
        first_week = week_nums[0]
        last_week = week_nums[-1]

        for block in blocks:
            block_plantings = [p for p in plantings if p.block_id == block.id]
            rows = []

            for p in block_plantings:
                plant_week = p.planned_plant_date.isocalendar()[1]
                harvest_start = p.planned_first_harvest_date.isocalendar()[1]
                harvest_end = p.planned_last_harvest_date.isocalendar()[1]

                # Position in the grid
                first_visible = max(first_week, plant_week)
                last_visible = min(last_week, harvest_end)

                if last_visible < first_visible:
                    continue  # Not visible in current window

                rows.append(
                    {
                        "planting": p,
                        "label": f"{p.crop.name}",
                        "sublabel": f"b{p.bed_start}-{p.bed_end}",
                        "col_start": first_visible - first_week,
                        "col_span": last_visible - first_visible + 1,
                        "plant_week": plant_week,
                        "harvest_start": harvest_start,
                        "harvest_end": harvest_end,
                        "status": p.status,
                        "css_class": self._status_css(p, weeks),
                    }
                )

            matrix[block.id] = rows

        return matrix

    def _status_css(self, planting, weeks):
        """Determine CSS class for planting bar."""
        current_week = date.today().isocalendar()[1]
        plant_wk = planting.planned_plant_date.isocalendar()[1]
        harvest_start = planting.planned_first_harvest_date.isocalendar()[1]
        harvest_end = planting.planned_last_harvest_date.isocalendar()[1]

        if planting.status == "failed":
            return "planting-failed"
        if planting.status == "revised":
            return "planting-revised"
        if planting.status == "complete":
            return "planting-complete"
        if current_week > harvest_end:
            return "planting-past"
        elif current_week >= harvest_start:
            return "planting-harvesting"
        elif current_week >= plant_wk:
            return "planting-growing"
        else:
            return "planting-planned"


class ActivePlanningYearMixin:
    """Redirect views that require an active planning year."""

    year_obj = None

    def dispatch(self, request, *args, **kwargs):
        self.year_obj = get_effective_planning_year(request)
        if not self.year_obj:
            messages.error(request, "No active planning year configured.")
            return redirect("planning:matrix")
        return super().dispatch(request, *args, **kwargs)


class PlantingDetailView(DetailView):
    """HTMX partial: planting detail panel."""

    model = Planting
    template_name = "planning/partials/planting_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        p = self.object
        ctx["nursery_events"] = p.nursery_events.all()
        ctx["harvest_events"] = p.harvest_events.all()[:8]
        ctx["field_walk_notes"] = p.field_walk_notes.order_by("-walk_date")[:5]

        # Rotation check
        from core.models import RotationRule, RotationHistory

        family = p.crop.botanical_family
        if family:
            rule = RotationRule.objects.filter(botanical_family=family).first()
            history = (
                RotationHistory.objects.filter(block=p.block, botanical_family=family)
                .order_by("-year")
                .first()
            )

            if rule and history:
                gap = p.planning_year.year - history.year
                ctx["rotation_warning"] = gap < rule.min_gap_years
                ctx["rotation_gap"] = gap
                ctx["rotation_min"] = rule.min_gap_years
                ctx["rotation_last_year"] = history.year

        # Yield summary if actuals exist
        actual_harvests = p.harvest_events.filter(actual_quantity__isnull=False)
        if actual_harvests.exists():
            from django.db.models import Sum

            ctx["total_actual_yield"] = actual_harvests.aggregate(total=Sum("actual_quantity"))[
                "total"
            ]
            ctx["yield_per_bedfoot"] = (
                ctx["total_actual_yield"] / p.planned_bedfeet if p.planned_bedfeet else None
            )

        return ctx


class PlantingFormContextMixin:
    """Shared form context for create/update/revise planting flows."""

    def _get_crop_season_choices(self, crop=None, block=None):
        if not crop or not block:
            return []
        return CropBySeason.objects.filter(
            crop=crop,
            block_type=block.block_type,
        ).order_by("dtm_days", "id")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx.get("form")
        selected_crop = None
        selected_block = None

        if form is not None:
            selected_crop = getattr(form.instance, "crop", None) or form.initial.get("crop")
            selected_block = getattr(form.instance, "block", None) or form.initial.get("block")

        ctx.update(
            {
                "crop_choices": CropInfo.objects.all().order_by("crop_type", "name"),
                "block_choices": Block.objects.all().order_by("block_type", "walk_route_order"),
                "selected_crop": selected_crop,
                "selected_block": selected_block,
                "crop_season_choices": self._get_crop_season_choices(selected_crop, selected_block),
                "is_htmx": bool(self.request.headers.get("HX-Request")),
            }
        )
        return ctx


class PlantingForm(forms.ModelForm):
    class Meta:
        model = Planting
        fields = [
            "crop",
            "crop_season",
            "variety",
            "block",
            "bed_start",
            "bed_end",
            "planned_plant_date",
        ]

    def clean(self):
        cleaned = super().clean()
        block = cleaned.get("block")
        bed_start = cleaned.get("bed_start")
        bed_end = cleaned.get("bed_end")
        crop = cleaned.get("crop")
        crop_season = cleaned.get("crop_season")

        if bed_start and bed_end and bed_end < bed_start:
            self.add_error("bed_end", "Bed end must be greater than or equal to bed start.")

        if block and bed_end and bed_end > block.num_beds:
            self.add_error("bed_end", f"{block.name} only has {block.num_beds} beds.")

        if block and crop and crop_season:
            if crop_season.crop_id != crop.id:
                self.add_error("crop_season", "Season profile must match the selected crop.")
            if crop_season.block_type != block.block_type:
                self.add_error("crop_season", "Season profile must match the selected block type.")

        return cleaned


class PlantingCreateView(ActivePlanningYearMixin, PlantingFormContextMixin, CreateView):
    """Create a new planting. Handles both full-page and HTMX partial."""

    model = Planting
    template_name = "planning/partials/planting_form.html"
    form_class = PlantingForm

    def get_initial(self):
        initial = super().get_initial()
        initial["planning_year"] = self.year_obj

        # Pre-fill from URL params (clicked cell in matrix)
        block_id = self.kwargs.get("block_id")
        week = self.kwargs.get("week")

        if block_id:
            block = Block.objects.get(id=block_id)
            initial["block"] = block
            initial["bed_start"] = 1
            initial["bed_end"] = block.num_beds

        if week and self.year_obj:
            initial["planned_plant_date"] = Week(self.year_obj.year, week).monday()

        return initial

    def form_valid(self, form):
        planting = form.save(commit=False)
        planting.planning_year = self.year_obj

        # Auto-calculate bedfeet
        block = planting.block
        beds = planting.bed_end - planting.bed_start + 1
        planting.planned_bedfeet = beds * block.bedfeet_per_bed

        planting.save()

        # Generate nursery and harvest events
        planting.generate_nursery_events()
        planting.generate_harvest_events()

        if self.request.headers.get("HX-Request"):
            # HTMX: return the detail panel for the new planting
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "plantingCreated",
                    "HX-Redirect": reverse("planning:matrix"),
                },
            )
        return redirect("planning:matrix")


class PlantingUpdateView(PlantingFormContextMixin, UpdateView):
    """Update a planting. Handles both full-page and HTMX partial."""

    model = Planting
    template_name = "planning/partials/planting_form.html"
    form_class = PlantingForm

    def get_initial(self):
        initial = super().get_initial()
        initial["planning_year"] = self.object.planning_year

        # Pre-fill from URL params (clicked cell in matrix)
        block_id = self.kwargs.get("block_id")
        week = self.kwargs.get("week")

        if block_id:
            block = Block.objects.get(id=block_id)
            initial["block"] = block
            initial["bed_start"] = 1
            initial["bed_end"] = block.num_beds

        if week:
            initial["planned_plant_date"] = Week(self.object.planning_year.year, week).monday()

        return initial

    def form_valid(self, form):
        planting = form.save(commit=False)
        planting.planning_year = self.object.planning_year

        # Auto-calculate bedfeet
        block = planting.block
        beds = planting.bed_end - planting.bed_start + 1
        planting.planned_bedfeet = beds * block.bedfeet_per_bed

        planting.save()

        # Regenerate only pending planned events so edits do not duplicate schedules.
        planting.nursery_events.filter(actual_date__isnull=True).delete()
        planting.harvest_events.filter(actual_quantity__isnull=True).delete()
        planting.generate_nursery_events()
        planting.generate_harvest_events()

        if self.request.headers.get("HX-Request"):
            # HTMX: return the detail panel for the new planting
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "plantingCreated",
                    "HX-Redirect": reverse("planning:matrix"),
                },
            )
        return redirect("planning:matrix")


class SuccessionPreviewView(View):
    """HTMX: show a preview table of what successions will be created."""

    def get(self, request):
        try:
            first_week = int(request.GET.get("first_plant_week", 0))
            last_week = int(request.GET.get("last_plant_week", 0))
            interval = int(request.GET.get("interval_weeks", 2))
            bf_per = int(request.GET.get("bedfeet_per_succession", 0))
            crop_id = request.GET.get("crop")
            block_id = request.GET.get("block")
            block_type = request.GET.get("block_type", "field")
            reuse = request.GET.get("reuse_beds") == "on"
        except (ValueError, TypeError):
            return HttpResponse('<span class="muted">Fill in valid values to preview.</span>')

        if not all([first_week, last_week, interval, bf_per, crop_id, block_id]):
            return HttpResponse(
                '<span class="muted">Fill in all required fields to preview.</span>'
            )

        if last_week < first_week or interval < 1:
            return HttpResponse('<span style="color:red;">Invalid week range or interval.</span>')

        try:
            crop = CropInfo.objects.get(id=crop_id)
            block = Block.objects.get(id=block_id)
            cs = CropBySeason.objects.get(crop=crop, block_type=block_type)
        except (CropInfo.DoesNotExist, Block.DoesNotExist, CropBySeason.DoesNotExist):
            return HttpResponse(
                '<span style="color:red;">'
                "No season profile found for this crop/block type combination."
                "</span>"
            )

        year_obj = get_effective_planning_year(request)
        year = year_obj.year if year_obj else date.today().year

        beds_per = math.ceil(bf_per / block.bedfeet_per_bed)

        # Generate succession list
        successions = []
        current_week = first_week
        num = 1

        while current_week <= last_week:
            plant_date = Week(year, current_week).monday()
            first_harvest = plant_date + timedelta(days=cs.dtm_days)
            last_harvest = first_harvest + timedelta(weeks=cs.harvest_weeks - 1)

            successions.append(
                {
                    "num": num,
                    "plant_week": current_week,
                    "plant_date": plant_date,
                    "harvest_start": first_harvest,
                    "harvest_start_week": first_harvest.isocalendar()[1],
                    "harvest_end": last_harvest,
                    "harvest_end_week": last_harvest.isocalendar()[1],
                }
            )

            current_week += interval
            num += 1

        # Assign beds (simplified preview)
        view = SuccessionCreateView()
        if reuse:
            successions = view._assign_beds_with_reuse(successions, block, beds_per, cs)
        else:
            successions = view._assign_beds_sequential(successions, block, beds_per)

        # Check capacity
        max_bed = max((s.get("bed_end", 0) for s in successions), default=0)
        over_capacity = max_bed > block.num_beds

        # Calculate totals
        total_bedfeet = len(successions) * bf_per
        total_yield = total_bedfeet * float(cs.total_yield_per_bedfoot)

        # Build preview HTML
        lines = [
            f'<div style="font-weight: bold; margin-bottom: 0.5rem;">'
            f"{len(successions)} successions · {total_bedfeet:,} total bedfeet · "
            f"~{total_yield:,.0f} {crop.harvest_unit} projected</div>"
        ]

        if over_capacity:
            lines.append(
                f'<div style="color: red; margin-bottom: 0.5rem;">'
                f"⚠ Requires bed {max_bed} but {block.name} has {block.num_beds} beds. "
                f"Enable bed reuse or reduce bedfeet per succession.</div>"
            )

        lines.append(
            '<table style="width:100%; border-collapse:collapse; font-size:0.8rem;">'
            '<thead><tr style="background:#eee;">'
            '<th style="padding:3px 6px;border:1px solid #ccc;">#</th>'
            '<th style="padding:3px 6px;border:1px solid #ccc;">Plant</th>'
            '<th style="padding:3px 6px;border:1px solid #ccc;">Beds</th>'
            '<th style="padding:3px 6px;border:1px solid #ccc;">First Harvest</th>'
            '<th style="padding:3px 6px;border:1px solid #ccc;">Last Harvest</th>'
            '<th style="padding:3px 6px;border:1px solid #ccc;">Yield</th>'
            "</tr></thead><tbody>"
        )

        for s in successions:
            bed_str = f"b{s.get('bed_start','?')}-{s.get('bed_end','?')}"
            over = s.get("bed_end", 0) > block.num_beds
            row_style = "background:#fff0f0;" if over else ""

            lines.append(
                f'<tr style="{row_style}">'
                f'<td style="padding:2px 6px;border:1px solid #e0e0e0;text-align:center;">{s["num"]}</td>'
                f'<td style="padding:2px 6px;border:1px solid #e0e0e0;">'
                f'Wk {s["plant_week"]} · {s["plant_date"].strftime("%b %-d")}</td>'
                f'<td style="padding:2px 6px;border:1px solid #e0e0e0;">{bed_str}</td>'
                f'<td style="padding:2px 6px;border:1px solid #e0e0e0;">'
                f'Wk {s["harvest_start_week"]} · {s["harvest_start"].strftime("%b %-d")}</td>'
                f'<td style="padding:2px 6px;border:1px solid #e0e0e0;">'
                f'Wk {s["harvest_end_week"]} · {s["harvest_end"].strftime("%b %-d")}</td>'
                f'<td style="padding:2px 6px;border:1px solid #e0e0e0;text-align:right;">'
                f"~{bf_per * float(cs.total_yield_per_bedfoot):,.0f} {crop.harvest_unit}</td>"
                f"</tr>"
            )

        lines.append("</tbody></table>")

        return HttpResponse("".join(lines))


class SuccessionForm(forms.Form):
    crop = forms.ModelChoiceField(queryset=CropInfo.objects.all())
    block_type = forms.ChoiceField(choices=BlockType.choices)
    block = forms.ModelChoiceField(queryset=Block.objects.all())
    bedfeet_per_succession = forms.IntegerField(min_value=1)
    first_plant_week = forms.IntegerField(min_value=1, max_value=52)
    last_plant_week = forms.IntegerField(min_value=1, max_value=52)
    interval_weeks = forms.IntegerField(min_value=1, max_value=8)
    reuse_beds = forms.BooleanField(required=False, initial=False)

    def clean(self):
        cleaned = super().clean()
        block = cleaned.get("block")
        crop = cleaned.get("crop")

        if block and crop:
            # Find matching crop_season profile
            try:
                cleaned["crop_season"] = CropBySeason.objects.get(
                    crop=crop,
                    block_type=cleaned["block_type"],
                )
            except CropBySeason.DoesNotExist:
                raise forms.ValidationError(
                    f"No season profile for {crop.name} in " f"{cleaned['block_type']} blocks."
                )
        return cleaned


class SuccessionCreateView(ActivePlanningYearMixin, FormView):
    template_name = "planning/succession_form.html"
    form_class = SuccessionForm

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            {
                "crop_choices": CropInfo.objects.all().order_by("crop_type", "name"),
                "block_choices": Block.objects.all().order_by("block_type", "walk_route_order"),
            }
        )
        return ctx

    def form_valid(self, form):
        data = form.cleaned_data

        crop = data["crop"]
        crop_season = data["crop_season"]
        block = data["block"]
        bf_per = data["bedfeet_per_succession"]
        first_week = data["first_plant_week"]
        last_week = data["last_plant_week"]
        interval = data["interval_weeks"]
        reuse = data["reuse_beds"]

        year = self.year_obj.year
        beds_per = math.ceil(bf_per / block.bedfeet_per_bed)

        # Generate succession list
        successions = []
        current_week = first_week
        succession_num = 1

        while current_week <= last_week:
            plant_date = Week(year, current_week).monday()

            harvest_start = plant_date + timedelta(days=crop_season.dtm_days)
            harvest_end = harvest_start + timedelta(weeks=crop_season.harvest_weeks - 1)

            successions.append(
                {
                    "num": succession_num,
                    "plant_week": current_week,
                    "plant_date": plant_date,
                    "harvest_start": harvest_start,
                    "harvest_end": harvest_end,
                    "harvest_start_week": harvest_start.isocalendar()[1],
                    "harvest_end_week": harvest_end.isocalendar()[1],
                }
            )

            current_week += interval
            succession_num += 1

        # Assign beds
        if reuse:
            successions = self._assign_beds_with_reuse(successions, block, beds_per, crop_season)
        else:
            successions = self._assign_beds_sequential(successions, block, beds_per)

        # Check if we exceed block capacity
        max_bed = max(s["bed_end"] for s in successions)
        if max_bed > block.num_beds:
            messages.error(
                self.request,
                f"Succession requires {max_bed} beds but {block.name} "
                f"only has {block.num_beds}. Reduce bedfeet, enable bed "
                f"reuse, or choose a larger block.",
            )
            return self.form_invalid(form)

        # Create the plantings
        group_id = f"{crop.name}-{block.name}-{year}"

        created = []
        for s in successions:
            bedfeet = (s["bed_end"] - s["bed_start"] + 1) * block.bedfeet_per_bed

            p = Planting.objects.create(
                planning_year=self.year_obj,
                crop=crop,
                crop_season=crop_season,
                block=block,
                bed_start=s["bed_start"],
                bed_end=s["bed_end"],
                planned_bedfeet=bedfeet,
                planned_plant_date=s["plant_date"],
                planned_first_harvest_date=s["harvest_start"],
                planned_last_harvest_date=s["harvest_end"],
                planned_total_yield=bedfeet * crop_season.total_yield_per_bedfoot,
                succession_group=group_id,
                status="planned",
            )
            p.generate_nursery_events()
            p.generate_harvest_events()
            created.append(p)

        messages.success(
            self.request,
            f"Created {len(created)} succession plantings of {crop.name} "
            f"in {block.name}, weeks {first_week}-{last_week}.",
        )
        return redirect("planning:matrix")

    def _assign_beds_sequential(self, successions, block, beds_per):
        """Each succession gets the next set of beds."""
        current_bed = 1
        for s in successions:
            s["bed_start"] = current_bed
            s["bed_end"] = current_bed + beds_per - 1
            current_bed += beds_per
        return successions

    def _assign_beds_with_reuse(self, successions, block, beds_per, crop_season):
        """Reuse beds when earlier successions finish."""
        # Track bed ranges and their availability
        # Each entry: (bed_start, bed_end, available_after_date)
        bed_slots = []

        for s in successions:
            plant_date = s["plant_date"]

            # Find a slot that's available by this plant date
            assigned = False
            for slot in bed_slots:
                if slot["available_after"] <= plant_date:
                    s["bed_start"] = slot["bed_start"]
                    s["bed_end"] = slot["bed_end"]
                    # Update slot availability to after THIS succession finishes
                    # Add 1 week buffer for cleanup/prep
                    slot["available_after"] = s["harvest_end"] + timedelta(weeks=1)
                    assigned = True
                    break

            if not assigned:
                # Need new beds
                if bed_slots:
                    next_start = max(sl["bed_end"] for sl in bed_slots) + 1
                else:
                    next_start = 1

                s["bed_start"] = next_start
                s["bed_end"] = next_start + beds_per - 1

                bed_slots.append(
                    {
                        "bed_start": next_start,
                        "bed_end": next_start + beds_per - 1,
                        "available_after": s["harvest_end"] + timedelta(weeks=1),
                    }
                )

        return successions


class NurseryScheduleView(ActivePlanningYearMixin, TemplateView):
    """Weekly nursery task view — seeding, pot up, transplant."""

    template_name = "planning/nursery_schedule.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year = self.year_obj.year

        requested_week = kwargs.get("week")
        if requested_week:
            center_week = requested_week
        else:
            center_week = date.today().isocalendar()[1]

        # Show 4-week window
        week_start = max(1, center_week - 1)
        week_end = min(52, week_start + 3)

        weeks_data = []

        for wk in range(week_start, week_end + 1):
            monday = Week(year, wk).monday()
            sunday = monday + timedelta(days=6)

            events = (
                NurseryEvent.objects.filter(
                    planting__planning_year=self.year_obj,
                    planned_date__gte=monday,
                    planned_date__lte=sunday,
                )
                .select_related("planting__crop", "planting__block")
                .order_by("event_type", "planting__crop__name")
            )

            seed_events = events.filter(event_type="seed")
            potup_events = events.filter(event_type="pot_up")
            transplant_events = events.filter(event_type="transplant")

            # Calculate bench space usage
            # All trays currently on benches this week:
            # seeded before this week AND not yet transplanted
            on_bench = (
                NurseryEvent.objects.filter(
                    planting__planning_year=self.year_obj,
                    event_type="seed",
                    planned_date__lte=sunday,
                )
                .exclude(
                    # Exclude if transplant has already happened
                    planting__nursery_events__event_type="transplant",
                    planting__nursery_events__planned_date__lt=monday,
                )
                .aggregate(total_trays=Sum("planned_tray_count"))["total_trays"]
                or 0
            )

            # Add pot-up trays (they replace seed trays but may be larger)
            potup_on_bench = (
                NurseryEvent.objects.filter(
                    planting__planning_year=self.year_obj,
                    event_type="pot_up",
                    planned_date__lte=sunday,
                )
                .exclude(
                    planting__nursery_events__event_type="transplant",
                    planting__nursery_events__planned_date__lt=monday,
                )
                .aggregate(total_trays=Sum("planned_tray_count"))["total_trays"]
                or 0
            )

            weeks_data.append(
                {
                    "week_num": wk,
                    "monday": monday,
                    "seed_events": seed_events,
                    "potup_events": potup_events,
                    "transplant_events": transplant_events,
                    "total_events": events.count(),
                    "bench_trays": on_bench + potup_on_bench,
                }
            )

        # Greenhouse capacity (could be a setting)
        greenhouse_capacity = 120  # trays

        # Peak bench usage across entire season for the chart
        bench_by_week = []
        for wk in range(1, 53):
            monday = Week(year, wk).monday()
            sunday = monday + timedelta(days=6)

            trays = (
                NurseryEvent.objects.filter(
                    planting__planning_year=self.year_obj,
                    event_type__in=["seed", "pot_up"],
                    planned_date__lte=sunday,
                )
                .exclude(
                    planting__nursery_events__event_type="transplant",
                    planting__nursery_events__planned_date__lt=monday,
                )
                .aggregate(total=Sum("planned_tray_count"))["total"]
                or 0
            )

            bench_by_week.append(
                {
                    "week": wk,
                    "trays": trays,
                    "pct": (
                        min(100, trays / greenhouse_capacity * 100) if greenhouse_capacity else 0
                    ),
                    "over_capacity": trays > greenhouse_capacity,
                }
            )

        peak_week = max(bench_by_week, key=lambda x: x["trays"])

        ctx.update(
            {
                "year": self.year_obj,
                "center_week": center_week,
                "weeks": weeks_data,
                "greenhouse_capacity": greenhouse_capacity,
                "bench_by_week": bench_by_week,
                "peak_week": peak_week,
            }
        )
        return ctx


class HarvestCalendarView(ActivePlanningYearMixin, TemplateView):
    """What's available each week across all plantings."""

    template_name = "planning/harvest_calendar.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year = self.year_obj.year

        # Build week range
        week_start = kwargs.get("week_start", 1)
        week_end = kwargs.get("week_end", 52)

        # Get all harvest events grouped by crop and week
        events = (
            HarvestEvent.objects.filter(
                planting__planning_year=self.year_obj,
            )
            .exclude(planting__status__in=["skipped", "failed", "revised"])
            .select_related("planting__crop", "planting__block")
            .order_by("planting__crop__name", "planned_date")
        )

        # Build matrix: crop_name → {week → total_qty}
        crop_weeks = {}
        all_crops = set()

        for he in events:
            crop_name = he.planting.crop.name
            wk = he.planned_date.isocalendar()[1]
            unit = he.planting.crop.harvest_unit

            if crop_name not in crop_weeks:
                crop_weeks[crop_name] = {
                    "crop": he.planting.crop,
                    "unit": unit,
                    "weeks": {},
                    "total": Decimal("0"),
                }

            qty = he.actual_quantity or he.planned_quantity or Decimal("0")
            crop_weeks[crop_name]["weeks"][wk] = (
                crop_weeks[crop_name]["weeks"].get(wk, Decimal("0")) + qty
            )
            crop_weeks[crop_name]["total"] += qty
            all_crops.add(crop_name)

        # Sort crops by type then name
        sorted_crops = sorted(
            crop_weeks.values(), key=lambda c: (c["crop"].crop_type, c["crop"].name)
        )

        # Weekly totals (count of crops available)
        weeks = list(range(week_start, week_end + 1))
        week_crop_counts = {}
        for wk in weeks:
            count = sum(1 for c in sorted_crops if wk in c["weeks"] and c["weeks"][wk] > 0)
            week_crop_counts[wk] = count

        ctx.update(
            {
                "year": self.year_obj,
                "crops": sorted_crops,
                "weeks": weeks,
                "week_crop_counts": week_crop_counts,
                "total_crops": len(all_crops),
                "current_week": date.today().isocalendar()[1],
            }
        )
        return ctx


class SalesPlanView(ActivePlanningYearMixin, TemplateView):
    """Product-by-week sales demand planning grid (301 semantics)."""

    template_name = "planning/sales_plan.html"

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)

        action = request.POST.get("action", "save")
        channel_id = request.POST.get("channel")
        if not channel_id:
            messages.error(request, "Channel is required.")
            return redirect(reverse("planning:sales_plan"))

        try:
            channel = SalesChannel.objects.get(id=channel_id)
        except SalesChannel.DoesNotExist:
            messages.error(request, "Invalid channel.")
            return redirect(reverse("planning:sales_plan"))

        if action == "save":
            updated = self._save_plan_rows(request, channel)
            messages.success(
                request,
                f"Saved {updated} planned product-week rows for {channel.name}.",
            )
            return redirect(f"{reverse('planning:sales_plan')}?channel={channel.id}")

        if action == "draft":
            from .services.sales_plan_translation import build_demand_to_supply_draft

            draft = build_demand_to_supply_draft(self.year_obj, channel)
            ctx = self.get_context_data(channel=channel, draft=draft)
            messages.info(
                request,
                "Generated demand-to-supply draft recommendations from current planned demand.",
            )
            return render(request, self.template_name, ctx)

        messages.error(request, "Unsupported action.")
        return redirect(f"{reverse('planning:sales_plan')}?channel={channel.id}")

    def _save_plan_rows(self, request, channel):
        updated = 0
        products = CropSalesFormat.objects.filter(is_active=True).select_related("crop")
        for product in products:
            for week in range(1, 53):
                key = f"qty_{product.id}_{week}"
                raw_value = (request.POST.get(key) or "").strip()
                sale_date = Week(self.year_obj.year, week).monday()

                existing = SalesEvent.objects.filter(
                    entry_kind=SalesEvent.EntryKind.PLAN,
                    planning_year=self.year_obj,
                    channel=channel,
                    product=product,
                    sale_date=sale_date,
                ).first()

                if not raw_value:
                    if existing:
                        existing.delete()
                    continue

                try:
                    quantity = Decimal(raw_value)
                except (InvalidOperation, TypeError):
                    continue

                revenue = quantity * product.sale_price
                SalesEvent.objects.update_or_create(
                    entry_kind=SalesEvent.EntryKind.PLAN,
                    planning_year=self.year_obj,
                    channel=channel,
                    product=product,
                    sale_date=sale_date,
                    defaults={
                        "planned_quantity": quantity,
                        "planned_revenue": revenue,
                        "notes": "Sales plan entry",
                    },
                )
                updated += 1
        return updated

    @staticmethod
    def _week_in_window(week: int, start_week: int, end_week: int) -> bool:
        """Return True when week falls within [start_week, end_week], handling wrap-around."""
        if start_week <= end_week:
            return start_week <= week <= end_week
        return week >= start_week or week <= end_week

    @classmethod
    def _window_state(
        cls,
        week: int,
        season_start_week: Optional[int],
        season_end_week: Optional[int],
        shoulder_weeks: int = 2,
    ) -> str:
        if not season_start_week or not season_end_week:
            return "unknown"
        if cls._week_in_window(week, season_start_week, season_end_week):
            return "harvest"
        for offset in range(1, shoulder_weeks + 1):
            before_week = ((week - offset - 1) % 52) + 1
            after_week = ((week + offset - 1) % 52) + 1
            if cls._week_in_window(before_week, season_start_week, season_end_week) or cls._week_in_window(
                after_week, season_start_week, season_end_week
            ):
                return "shoulder"
        return "off"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        channel = kwargs.get("channel")
        channels = SalesChannel.objects.order_by("allocation_priority", "name")
        if channel is None:
            selected_channel_id = self.request.GET.get("channel")
            channel = channels.filter(id=selected_channel_id).first() if selected_channel_id else channels.first()

        products = CropSalesFormat.objects.filter(is_active=True).select_related("crop").order_by(
            "crop__crop_type", "crop__name", "product_name"
        )
        weeks = list(range(1, 53))
        crop_ids = [product.crop_id for product in products]
        season_profiles = (
            CropBySeason.objects.filter(crop_id__in=crop_ids)
            .order_by("crop_id", "block_type", "id")
        )
        seasons_by_crop = {}
        for profile in season_profiles:
            seasons_by_crop.setdefault(profile.crop_id, []).append(profile)

        planned_lookup = {}
        summary_qty = Decimal("0")
        summary_revenue = Decimal("0")

        if channel:
            rows = SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
                channel=channel,
            ).select_related("product")
            for row in rows:
                week = row.sale_date.isocalendar()[1]
                key = (row.product_id, week)
                planned_lookup[key] = row
                summary_qty += row.planned_quantity or Decimal("0")
                summary_revenue += row.planned_revenue or Decimal("0")

        product_rows = []
        for product in products:
            crop_profiles = seasons_by_crop.get(product.crop_id, [])
            selected_profile = next(
                (profile for profile in crop_profiles if profile.block_type == BlockType.FIELD),
                crop_profiles[0] if crop_profiles else None,
            )
            season_start = selected_profile.field_week_start if selected_profile else None
            season_end = selected_profile.field_week_end if selected_profile else None
            window_label = (
                f"Wk {season_start}-{season_end}" if season_start and season_end else "No harvest profile"
            )

            week_cells = []
            for week in weeks:
                plan = planned_lookup.get((product.id, week))
                window_state = self._window_state(week, season_start, season_end)
                css_class = f"sales-plan-cell sales-plan-cell--{window_state}"
                if plan and plan.planned_quantity:
                    css_class += " sales-plan-cell--planned"
                week_cells.append(
                    {
                        "week": week,
                        "planned_quantity": plan.planned_quantity if plan else None,
                        "window_state": window_state,
                        "css_class": css_class,
                        "title": f"{window_label} · Week {week}",
                    }
                )
            product_rows.append(
                {
                    "product": product,
                    "is_mix_product": product.is_mix_product,
                    "active_recipe": product.recipes.filter(is_active=True).first(),
                    "week_cells": week_cells,
                    "season_profile": selected_profile,
                    "season_window_label": window_label,
                }
            )

        ctx.update(
            {
                "year": self.year_obj,
                "channels": channels,
                "channel": channel,
                "product_rows": product_rows,
                "weeks": weeks,
                "summary_qty": summary_qty,
                "summary_revenue": summary_revenue,
                "draft": kwargs.get("draft"),
            }
        )
        return ctx


class PlantingReviseView(View):
    """Mark existing planting as revised, create a new one as its replacement.

    The original is kept for historical comparison.
    The new planting points back to the original via revision_of.
    """

    template_name = "planning/partials/planting_form.html"

    def get(self, request, pk):
        original = get_object_or_404(Planting, pk=pk)

        # Pre-populate form with original values
        initial = {
            "crop": original.crop,
            "crop_season": original.crop_season,
            "variety": original.variety,
            "block": original.block,
            "bed_start": original.bed_start,
            "bed_end": original.bed_end,
            "planned_plant_date": original.planned_plant_date,
            "succession_group": original.succession_group,
            "notes": f"Revision of planting #{original.id}: {original.notes}",
        }

        ctx = self._build_context(request, original, initial)
        return render(request, self.template_name, ctx)

    def post(self, request, pk):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        original = get_object_or_404(Planting, pk=pk)
        year_obj = original.planning_year

        crop_id = request.POST.get("crop")
        crop_season_id = request.POST.get("crop_season")
        block_id = request.POST.get("block")

        try:
            crop = CropInfo.objects.get(id=crop_id)
            crop_season = CropBySeason.objects.get(id=crop_season_id)
            block = Block.objects.get(id=block_id)
        except (CropInfo.DoesNotExist, CropBySeason.DoesNotExist, Block.DoesNotExist):
            messages.error(request, "Invalid crop, season, or block.")
            return redirect("planning:planting_edit", pk=pk)

        try:
            bed_start = int(request.POST.get("bed_start", 1))
            bed_end = int(request.POST.get("bed_end", 1))
            plant_date_str = request.POST.get("planned_plant_date")
            plant_date = date.fromisoformat(plant_date_str)
        except (TypeError, ValueError):
            messages.error(request, "Invalid bed range or plant date.")
            return redirect("planning:planting_revise", pk=pk)

        bedfeet = (bed_end - bed_start + 1) * block.bedfeet_per_bed
        first_harvest = plant_date + timedelta(days=crop_season.dtm_days)
        last_harvest = first_harvest + timedelta(weeks=crop_season.harvest_weeks - 1)
        planned_yield = bedfeet * crop_season.total_yield_per_bedfoot

        # Mark original as revised
        original.status = "revised"
        original.notes += f"\nRevised on {date.today()}"
        original.save()

        # Cancel original's pending nursery events.
        original.nursery_events.filter(actual_date__isnull=True).delete()

        # Cancel original's future harvest events
        original.harvest_events.filter(
            planned_date__gt=date.today(),
            actual_quantity__isnull=True,
        ).delete()

        # Create revised planting
        revised = Planting.objects.create(
            planning_year=year_obj,
            revision_of=original,
            crop=crop,
            crop_season=crop_season,
            variety=request.POST.get("variety", ""),
            block=block,
            bed_start=bed_start,
            bed_end=bed_end,
            planned_bedfeet=bedfeet,
            planned_plant_date=plant_date,
            planned_first_harvest_date=first_harvest,
            planned_last_harvest_date=last_harvest,
            planned_total_yield=planned_yield,
            succession_group=request.POST.get("succession_group", ""),
            status="planned",
            notes=request.POST.get("notes", ""),
        )

        revised.generate_nursery_events()
        revised.generate_harvest_events()

        messages.success(
            request,
            f"Revision created: {revised.crop.name} in {revised.block.name}. "
            f"Original planting #{original.id} marked as revised.",
        )

        if request.headers.get("HX-Request"):
            return HttpResponse(
                status=204,
                headers={"HX-Trigger": "plantingRevised"},
            )

        return redirect("planning:planting_detail", pk=revised.id)

    def _build_context(self, request, original, initial):
        crops = CropInfo.objects.all().order_by("crop_type", "name")
        blocks = Block.objects.all().order_by("block_type", "walk_route_order")

        crop_season_choices = (
            CropBySeason.objects.filter(
                crop=original.crop,
                block_type=original.block.block_type,
            )
            if original
            else []
        )

        return {
            "is_htmx": bool(request.headers.get("HX-Request")),
            "is_revision": True,
            "original": original,
            "crop_choices": crops,
            "block_choices": blocks,
            "crop_season_choices": crop_season_choices,
            "selected_crop": original.crop,
            "selected_block": original.block,
            "form": type(
                "Form",
                (),
                {
                    field: type("Field", (), {"value": lambda s, v=val: v})()
                    for field, val in initial.items()
                },
            )(),
        }


class PlantingStatusUpdateView(View):
    """HTMX: quick status update without full form."""

    def post(self, request, pk):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        planting = get_object_or_404(Planting, pk=pk)
        new_status = request.POST.get("status")

        valid_statuses = [s[0] for s in PlantingStatus.choices]
        if new_status not in valid_statuses:
            return HttpResponse(status=400)

        old_status = planting.status
        allowed_transitions = {
            "planned": {"planned", "seeded", "planted", "failed", "skipped", "revised"},
            "seeded": {"seeded", "planted", "failed", "skipped", "revised"},
            "planted": {"planted", "growing", "harvesting", "failed", "revised"},
            "growing": {"growing", "harvesting", "failed", "revised"},
            "harvesting": {"harvesting", "complete", "failed", "revised"},
            "complete": {"complete", "revised"},
            "failed": {"failed", "revised"},
            "skipped": {"skipped", "revised"},
            "revised": {"revised", "planned"},
        }
        if new_status not in allowed_transitions.get(old_status, {old_status}):
            return HttpResponse(status=400)

        planting.status = new_status

        # Auto-set dates based on status transitions
        today = date.today()

        if new_status == "planted" and old_status == "planned":
            if not planting.actual_plant_date:
                planting.actual_plant_date = today

        elif new_status == "harvesting" and old_status in ("planted", "growing"):
            if not planting.actual_first_harvest_date:
                planting.actual_first_harvest_date = today

        elif new_status == "complete":
            if not planting.actual_last_harvest_date:
                planting.actual_last_harvest_date = today

        planting.save()

        messages.success(request, f"{planting.crop.name} status: {old_status} → {new_status}")

        # Return updated detail panel
        if request.headers.get("HX-Request"):
            return redirect("planning:planting_detail_htmx", pk=pk)

        return redirect("planning:matrix")


# planning/views.py (FieldScheduleView)


class FieldScheduleView(ActivePlanningYearMixin, TemplateView):
    """Week-by-week field tasks: plantings, terminations, bed prep."""

    template_name = "planning/field_schedule.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        year = self.year_obj.year

        week_num = kwargs.get("week", date.today().isocalendar()[1])
        week_start = max(1, week_num - 1)
        week_end = min(52, week_start + 5)

        weeks_data = []

        for wk in range(week_start, week_end + 1):
            monday = Week(year, wk).monday()
            sunday = monday + timedelta(days=6)

            # Plantings starting this week
            planting_this_week = (
                Planting.objects.filter(
                    planning_year=self.year_obj,
                    planned_plant_date__gte=monday,
                    planned_plant_date__lte=sunday,
                )
                .exclude(status__in=["skipped", "failed", "revised"])
                .select_related("crop", "crop_season", "block")
                .order_by("block__walk_route_order", "bed_start")
            )

            # Direct seeded plantings (no nursery)
            direct_seed_this_week = planting_this_week.filter(crop__nursery_weeks=0)

            # Transplants this week (from nursery events)
            transplant_this_week = (
                NurseryEvent.objects.filter(
                    planting__planning_year=self.year_obj,
                    event_type="transplant",
                    planned_date__gte=monday,
                    planned_date__lte=sunday,
                )
                .select_related("planting__crop", "planting__block", "planting__crop_season")
                .order_by("planting__block__walk_route_order", "planting__bed_start")
            )

            # Beds finishing this week (last harvest)
            finishing_this_week = (
                Planting.objects.filter(
                    planning_year=self.year_obj,
                    planned_last_harvest_date__gte=monday,
                    planned_last_harvest_date__lte=sunday,
                )
                .exclude(status__in=["skipped", "failed", "revised"])
                .select_related("crop", "block")
                .order_by("block__walk_route_order", "bed_start")
            )

            # Beds freed this week (available for replanting)
            beds_freed = []
            for p in finishing_this_week:
                beds_freed.append(
                    {
                        "block": p.block,
                        "bed_start": p.bed_start,
                        "bed_end": p.bed_end,
                        "bedfeet": p.planned_bedfeet,
                        "prev_crop": p.crop.name,
                        "family": p.crop.botanical_family,
                    }
                )

            # Infrastructure tasks this week
            # (trellising, irrigation setup for plantings)
            trellis_tasks = []
            for p in planting_this_week:
                if p.crop_season.trellis_system:
                    trellis_tasks.append(
                        {
                            "block": p.block.name,
                            "beds": f"b{p.bed_start}-{p.bed_end}",
                            "crop": p.crop.name,
                            "system": p.crop_season.trellis_system,
                        }
                    )

            mulch_tasks = []
            for p in planting_this_week:
                if p.crop_season.mulch:
                    mulch_tasks.append(
                        {
                            "block": p.block.name,
                            "beds": f"b{p.bed_start}-{p.bed_end}",
                            "crop": p.crop.name,
                            "mulch": p.crop_season.mulch,
                        }
                    )

            weeks_data.append(
                {
                    "week_num": wk,
                    "monday": monday,
                    "sunday": sunday,
                    "is_current": (wk == date.today().isocalendar()[1]),
                    "direct_seed": direct_seed_this_week,
                    "transplants": transplant_this_week,
                    "finishing": finishing_this_week,
                    "beds_freed": beds_freed,
                    "trellis_tasks": trellis_tasks,
                    "mulch_tasks": mulch_tasks,
                    "total_tasks": (
                        direct_seed_this_week.count()
                        + transplant_this_week.count()
                        + len(trellis_tasks)
                        + len(mulch_tasks)
                    ),
                    "bedfeet_going_in": sum(p.planned_bedfeet for p in planting_this_week),
                    "bedfeet_coming_out": sum(b["bedfeet"] for b in beds_freed),
                }
            )

        ctx.update(
            {
                "year": self.year_obj,
                "week_num": week_num,
                "weeks": weeks_data,
                "prev_start": max(1, week_start - 4),
                "next_start": min(52, week_end + 1),
            }
        )
        return ctx


# (HTMX helper views)
class CropSeasonOptionsView(View):
    """HTMX: return <option> elements for crop_season select."""

    def get(self, request):
        crop_id = request.GET.get("crop")
        block_id = request.GET.get("block")

        options = []

        if crop_id and block_id:
            try:
                block = Block.objects.get(id=block_id)
                seasons = CropBySeason.objects.filter(
                    crop_id=crop_id,
                    block_type=block.block_type,
                )
                for cs in seasons:
                    crop = cs.crop
                    options.append(
                        f'<option value="{cs.id}">'
                        f"{cs.get_block_type_display()} — "
                        f"DTM {cs.dtm_days}d · "
                        f"{cs.harvest_weeks}wk harvest · "
                        f"{cs.total_yield_per_bedfoot}{crop.harvest_unit}/bf"
                        f"</option>"
                    )
            except (Block.DoesNotExist, ValueError):
                pass

        if not options:
            options = ['<option value="">— select crop and block first —</option>']

        return HttpResponse("".join(options))


class HarvestDateCalcView(View):
    """HTMX: return calculated harvest dates as HTML fragment."""

    def get(self, request):
        crop_season_id = request.GET.get("crop_season")
        plant_date_str = request.GET.get("planned_plant_date")

        if not crop_season_id or not plant_date_str:
            return HttpResponse('<span class="muted">Select crop season and plant date.</span>')

        try:
            cs = CropBySeason.objects.select_related("crop").get(id=crop_season_id)
            plant_date = date.fromisoformat(plant_date_str)

            first_harvest = plant_date + timedelta(days=cs.dtm_days)
            last_harvest = first_harvest + timedelta(weeks=cs.harvest_weeks - 1)

            seed_date = None
            if cs.crop.nursery_weeks:
                seed_date = plant_date - timedelta(weeks=cs.crop.nursery_weeks)

            parts = [
                f"<dt>First harvest:</dt>"
                f'<dd>{first_harvest.strftime("%b %-d")} '
                f"(Wk {first_harvest.isocalendar()[1]})</dd>",
                f"<dt>Last harvest:</dt>"
                f'<dd>{last_harvest.strftime("%b %-d")} '
                f"(Wk {last_harvest.isocalendar()[1]})</dd>",
            ]

            if seed_date:
                parts.append(
                    f"<dt>Seed date:</dt>"
                    f'<dd>{seed_date.strftime("%b %-d")} '
                    f"(Wk {seed_date.isocalendar()[1]})</dd>"
                )

            return HttpResponse("".join(parts))

        except (CropBySeason.DoesNotExist, ValueError):
            return HttpResponse('<span class="muted">Invalid crop season or date.</span>')


class BedfeetCalcView(View):
    """HTMX: return calculated bedfeet and yield as HTML fragment."""

    def get(self, request):
        block_id = request.GET.get("block")
        bed_start = request.GET.get("bed_start")
        bed_end = request.GET.get("bed_end")
        crop_season_id = request.GET.get("crop_season")

        if not all([block_id, bed_start, bed_end]):
            return HttpResponse('<span class="muted">Select block and beds.</span>')

        try:
            block = Block.objects.get(id=block_id)
            start = int(bed_start)
            end = int(bed_end)

            if end < start:
                return HttpResponse('<span style="color:red;">Bed end must be ≥ bed start.</span>')
            if end > block.num_beds:
                return HttpResponse(
                    f'<span style="color:red;">'
                    f"{block.name} only has {block.num_beds} beds.</span>"
                )

            num_beds = end - start + 1
            bedfeet = num_beds * block.bedfeet_per_bed

            parts = [
                f"<dt>Bedfeet:</dt><dd>{bedfeet:,} bf</dd>"
                f"<dt>Beds:</dt><dd>{num_beds} beds × {block.bedfeet_per_bed}bf</dd>"
            ]

            if crop_season_id:
                try:
                    cs = CropBySeason.objects.select_related("crop").get(id=crop_season_id)
                    planned_yield = bedfeet * float(cs.total_yield_per_bedfoot)
                    weekly_yield = planned_yield / cs.harvest_weeks

                    parts.extend(
                        [
                            f"<dt>Planned yield:</dt>"
                            f"<dd>{planned_yield:,.0f} {cs.crop.harvest_unit}"
                            f" ({weekly_yield:,.0f}/wk)</dd>",
                        ]
                    )

                    if cs.crop.units_per_bin:
                        total_bins = planned_yield / cs.crop.units_per_bin
                        weekly_bins = weekly_yield / cs.crop.units_per_bin
                        parts.append(
                            f"<dt>Est. bins:</dt>"
                            f"<dd>{total_bins:.1f} total "
                            f"({weekly_bins:.1f}/wk {cs.crop.harvest_bin})</dd>"
                        )

                    if cs.tp_inrow_spacing and cs.rows_per_bed:
                        plants = bedfeet * cs.rows_per_bed / float(cs.tp_inrow_spacing)
                        parts.append(f"<dt>Plants:</dt><dd>~{int(plants):,}</dd>")

                except CropBySeason.DoesNotExist:
                    pass

            return HttpResponse("".join(parts))

        except (Block.DoesNotExist, ValueError):
            return HttpResponse('<span class="muted">Invalid selection.</span>')


class WeekToDateView(View):
    """HTMX: convert week number input to date value for date field."""

    def get(self, request):
        week_num = request.GET.get("plant_week_input")
        year_obj = get_effective_planning_year(request)

        if not week_num or not year_obj:
            return HttpResponse("")

        try:
            wk = int(week_num)
            if not (1 <= wk <= 52):
                return HttpResponse("")

            monday = Week(year_obj.year, wk).monday()
            # Return as date input value
            return HttpResponse(
                f'<input type="date" name="planned_plant_date" '
                f'id="id_planned_plant_date" '
                f'value="{monday.isoformat()}" required '
                f'hx-get="{reverse("planning:harvest_date_calc")}" '
                f'hx-target="#calc-dates" '
                f'hx-trigger="change" '
                f"hx-include=\"[name='crop_season']\">"
            )
        except (ValueError, TypeError):
            return HttpResponse("")


class BedConflictCheckView(View):
    """HTMX: check if proposed beds conflict with existing plantings."""

    def get(self, request):
        block_id = request.GET.get("block")
        bed_start = request.GET.get("bed_start")
        bed_end = request.GET.get("bed_end")
        plant_date_str = request.GET.get("planned_plant_date")
        crop_season_id = request.GET.get("crop_season")
        planting_id = request.GET.get("planting_id")  # for edits

        if not all([block_id, bed_start, bed_end, plant_date_str, crop_season_id]):
            return HttpResponse("")

        try:
            start = int(bed_start)
            end = int(bed_end)
            plant_date = date.fromisoformat(plant_date_str)
            cs = CropBySeason.objects.get(id=crop_season_id)

            first_harvest = plant_date + timedelta(days=cs.dtm_days)
            last_harvest = first_harvest + timedelta(weeks=cs.harvest_weeks - 1)

            year_obj = get_effective_planning_year(request)
            if not year_obj:
                return HttpResponse("")

            conflicts = (
                Planting.objects.filter(
                    planning_year=year_obj,
                    block_id=block_id,
                    bed_start__lte=end,
                    bed_end__gte=start,
                    planned_last_harvest_date__gte=plant_date,
                    planned_plant_date__lte=last_harvest,
                )
                .exclude(status__in=["skipped", "failed", "revised"])
                .select_related("crop")
            )

            if planting_id:
                conflicts = conflicts.exclude(id=planting_id)

            if not conflicts.exists():
                return HttpResponse('<span style="color: #166534;">✓ No conflicts</span>')

            parts = ['<div class="warning"><strong>⚠ Bed Conflicts:</strong>']
            for c in conflicts:
                parts.append(
                    f"<br>· {c.crop.name} ({c.status}) "
                    f"beds {c.bed_start}-{c.bed_end}, "
                    f'{c.planned_plant_date.strftime("%b %-d")} – '
                    f'{c.planned_last_harvest_date.strftime("%b %-d")}'
                )
            parts.append("</div>")

            return HttpResponse("".join(parts))

        except (CropBySeason.DoesNotExist, ValueError):
            return HttpResponse("")
