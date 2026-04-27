"""planning.views"""

import math

from django.shortcuts import get_object_or_404, render
from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages
from django.views.generic import TemplateView
from django.db.models import Max, Q, Sum
from isoweek import Week

from core.operator_scope import (
    active_crop_info_for_planning_year,
    active_crop_sales_formats_for_planning_year,
    operator_sales_channels,
)
from reference.models import (
    Block,
    BlockType,
    CropBySeason,
    CropInfo,
    CropSalesFormat,
    SalesCategory,
    SalesChannel,
)
from reference.sales_rollups import (
    DEFAULT_ROLLUP_SLUG,
    ROLLUP_PLAN_CHANNEL_NAMES,
    ROLLUP_SLUG_TO_CATEGORY_NAME,
    ROLLUP_SLUG_TO_CHANNEL_NAME,
    ROLLUP_TAB_LABELS,
    plan_events_without_shadowed_rollups,
)
from planning.services.sales_plan_allocation import even_split_sale_units
from operations.planting_display import (
    planting_schedule_chip_css_class,
    planting_unit_code,
    planting_unit_matrix_sublabel,
    planting_variety_display,
)
from .models import Planting, HarvestEvent, NurseryEvent, PlantingStatus, PlanningYear
from core.planning_year import get_effective_planning_year, set_session_planning_year
from core.ui_diagnostics import (
    nursery_surface_hints,
    sales_plan_product_scope_explanations,
    sales_plan_surface_hints,
)
from operations.services.week_ops import EXCLUDED_PLANTING_STATUSES
from .services.planting_events_repair import count_plantings_missing_harvest_events
from django.views.generic import DetailView, CreateView, UpdateView, View, FormView
from sales.models import SalesEvent

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django import forms
from django.template.loader import render_to_string
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Optional
from collections import defaultdict


def _planning_matrix_week_strip(year: int, requested_date, requested_week, center_fallback: date | None = None):
    """Return the same `weeks` list of dicts used by PlanningMatrixView (16-week window)."""
    center_date = center_fallback or date.today()
    if requested_date:
        center_date = date.fromisoformat(str(requested_date))
    elif requested_week is not None:
        center_date = Week(year, int(requested_week)).monday()

    window_start_date = center_date - timedelta(weeks=4)
    window_end_date = center_date + timedelta(weeks=11)
    weeks = []
    current = window_start_date
    while current <= window_end_date:
        weeks.append(
            {
                "date": current,
                "num": current.isocalendar()[1],
                "is_current": current.isocalendar()[1] == date.today().isocalendar()[1],
            }
        )
        current += timedelta(weeks=1)
    return weeks


def _matrix_row_dict_for_planting(planting: Planting, weeks: list, _year: int):
    """Single planting row for `render_planting_bar`, or None if outside the visible week window."""
    week_nums = [w["num"] for w in weeks]
    first_week = week_nums[0]
    last_week = week_nums[-1]
    plant_week = planting.planned_plant_date.isocalendar()[1]
    harvest_end = planting.planned_last_harvest_date.isocalendar()[1]
    first_visible = max(first_week, plant_week)
    last_visible = min(last_week, harvest_end)
    if last_visible < first_visible:
        return None
    vlabel = planting_variety_display(planting)
    vpart = f" — {vlabel}" if vlabel else ""
    return {
        "planting": planting,
        "label": f"{planting.crop.name}{vpart}",
        "sublabel": planting_unit_matrix_sublabel(planting),
        "col_start": first_visible - first_week,
        "col_span": last_visible - first_visible + 1,
        "plant_week": plant_week,
        "harvest_start": planting.planned_first_harvest_date.isocalendar()[1],
        "harvest_end": harvest_end,
        "status": planting.status,
        "css_class": PlanningMatrixView.status_css_for_planting(planting, weeks),
    }


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
        window_end_date = center_date + timedelta(weeks=11)  # 16 week window

        weeks = _planning_matrix_week_strip(year, None, None, center_date)

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
            .select_related("crop", "crop_season", "block", "variety_obj")
            .order_by("block__name", "bed_start", "planned_plant_date")
        )

        # Build the matrix: block → list of plantings with week positions
        matrix = self._build_matrix(blocks, plantings, weeks, year)
        displayed_blocks = list(field_blocks) + list(tunnel_blocks)
        active_block_count = sum(1 for block in displayed_blocks if matrix.get(block.id))

        # Extract week number range for display
        week_nums = [w['num'] for w in weeks]

        max_year = PlanningYear.objects.aggregate(m=Max("year"))["m"]
        next_planning_year_row = PlanningYear.objects.filter(year=year_obj.year + 1).first()
        planning_years_qs = PlanningYear.objects.all().order_by("year")

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
                "crop_planner_summary": {
                    "visible_planting_count": plantings.count(),
                    "active_block_count": active_block_count,
                    "total_block_count": len(displayed_blocks),
                },
                "prev_date": window_start_date - timedelta(weeks=8),
                "next_date": window_end_date + timedelta(days=1),
                "matrix_center_date": center_date.isoformat(),
                # YP-1 / YP-4: next-season CTA when viewing the latest planning year in the DB
                "next_planning_year_row": next_planning_year_row,
                "show_start_next_season_cta": max_year is not None and year_obj.year == max_year,
                "planning_years": list(planning_years_qs),
                "is_latest_planning_year": max_year is not None and year_obj.year == max_year,
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
                row = _matrix_row_dict_for_planting(p, weeks, year)
                if row:
                    rows.append(row)

            matrix[block.id] = rows

        return matrix

    @staticmethod
    def status_css_for_planting(planting, weeks):
        """Determine CSS class for planting bar (shared with drag-move fragment)."""
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

    def _status_css(self, planting, weeks):
        return self.status_css_for_planting(planting, weeks)


def _planting_display_variety_label(planting: Planting) -> str:
    if getattr(planting, "variety_obj_id", None) and planting.variety_obj:
        return planting.variety_obj.name
    return (planting.variety or "").strip()


def _regenerate_planting_events(planting: Planting) -> None:
    """Recompute harvest window from planned_plant_date + crop_season; refresh pending events."""
    cs = planting.crop_season
    planting.planned_first_harvest_date = planting.planned_plant_date + timedelta(days=cs.dtm_days)
    planting.planned_last_harvest_date = planting.planned_first_harvest_date + timedelta(
        weeks=cs.harvest_weeks - 1
    )
    planting.planned_total_yield = planting.planned_bedfeet * cs.total_yield_per_bedfoot
    planting.save()
    planting.nursery_events.filter(actual_date__isnull=True).delete()
    planting.harvest_events.filter(actual_quantity__isnull=True).delete()
    planting.generate_nursery_events()
    planting.generate_harvest_events()


@method_decorator(csrf_protect, name="dispatch")
class PlantingMoveView(View):
    """POST JSON: move planting to another block and/or ISO week (crop planner drag-and-drop)."""

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "auth"}, status=401)
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

        import json

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

        planting_id = payload.get("planting_id")
        block_id = payload.get("block_id")
        week = payload.get("week")
        try:
            planting_id = int(planting_id)
            block_id = int(block_id)
            week = int(week)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "bad_ids"}, status=400)

        if not (1 <= week <= 52):
            return JsonResponse({"ok": False, "error": "bad_week"}, status=400)

        year_obj = get_effective_planning_year(request)
        if not year_obj:
            return JsonResponse({"ok": False, "error": "no_year"}, status=400)

        planting = get_object_or_404(
            Planting.objects.select_related("crop", "crop_season", "block", "variety_obj"),
            pk=planting_id,
            planning_year=year_obj,
        )
        new_block = get_object_or_404(Block, pk=block_id)
        old_block_id = planting.block_id

        try:
            new_plant_date = Week(year_obj.year, week).monday()
        except Exception:
            return JsonResponse({"ok": False, "error": "bad_week_date"}, status=400)

        planting.block = new_block
        planting.planned_plant_date = new_plant_date
        _regenerate_planting_events(planting)

        warnings = []
        conflicts = (
            Planting.objects.filter(
                planning_year=year_obj,
                block_id=planting.block_id,
                bed_start__lte=planting.bed_end,
                bed_end__gte=planting.bed_start,
                planned_last_harvest_date__gte=planting.planned_plant_date,
                planned_plant_date__lte=planting.planned_last_harvest_date,
            )
            .exclude(status__in=["skipped", "failed", "revised"])
            .exclude(pk=planting.pk)
            .select_related("crop")
        )
        conflict_list = list(conflicts[:9])
        for c in conflict_list[:8]:
            warnings.append(
                f"{c.crop.name} {_planting_display_variety_label(c)} b{c.bed_start}-{c.bed_end} "
                f"({c.planned_plant_date} – {c.planned_last_harvest_date})"
            )
        total_c = conflicts.count()
        if total_c > 8:
            warnings.append(f"…and {total_c - 8} more overlaps")

        reload_needed = old_block_id != planting.block_id
        row_html = ""
        if not reload_needed:
            matrix_date = payload.get("matrix_date")
            matrix_week = payload.get("matrix_week")
            try:
                mw = int(matrix_week) if matrix_week is not None else None
            except (TypeError, ValueError):
                mw = None
            weeks_ctx = _planning_matrix_week_strip(year_obj.year, matrix_date, mw, None)
            planting.refresh_from_db()
            planting = Planting.objects.select_related("crop", "crop_season", "block", "variety_obj").get(
                pk=planting.pk
            )
            row_dict = _matrix_row_dict_for_planting(planting, weeks_ctx, year_obj.year)
            if row_dict is None:
                reload_needed = True
            else:
                from planning.templatetags import planning_tags

                row_html = str(planning_tags.render_planting_bar(row_dict, weeks_ctx, year_obj.year))

        return JsonResponse(
            {
                "ok": True,
                "warnings": warnings,
                "reload": reload_needed,
                "html": row_html,
            }
        )


@method_decorator(csrf_protect, name="dispatch")
class PlantingDeleteView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST JSON: delete a planting (crop planner drag-to-trash)."""

    raise_exception = True

    def test_func(self):
        u = self.request.user
        return u.is_authenticated and u.is_staff

    def post(self, request, *args, **kwargs):
        import json

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

        try:
            planting_id = int(payload.get("planting_id"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "bad_id"}, status=400)

        year_obj = get_effective_planning_year(request)
        if not year_obj:
            return JsonResponse({"ok": False, "error": "no_year"}, status=400)

        planting = get_object_or_404(Planting, pk=planting_id, planning_year=year_obj)
        planting.delete()
        return JsonResponse({"ok": True})


class StaffPlanningMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Staff-only planning mutations (prototype gate)."""

    raise_exception = True

    def test_func(self):
        u = self.request.user
        return u.is_authenticated and u.is_staff


@method_decorator(csrf_protect, name="dispatch")
class PlanningYearSetView(StaffPlanningMixin, View):
    """POST: set session planning year from matrix (YP-4)."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        next_url = request.POST.get("next") or reverse("planning:matrix")
        if not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = reverse("planning:matrix")

        try:
            year_id = int(request.POST.get("planning_year_id", ""))
        except (TypeError, ValueError):
            messages.error(request, "Invalid planning year.")
            return redirect(next_url)

        planning_year = PlanningYear.objects.filter(pk=year_id).first()
        if planning_year is None:
            messages.error(request, "That planning year does not exist.")
            return redirect(next_url)

        set_session_planning_year(request, planning_year)
        messages.success(request, f"Switched to planning year {planning_year.year}.")
        return redirect(next_url)


class SeasonRolloverPreviewView(StaffPlanningMixin, TemplateView):
    """YP-5: review skeleton copy counts before commit."""

    template_name = "planning/season_rollover_preview.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        source = get_effective_planning_year(self.request)
        if not source:
            ctx["no_year"] = True
            return ctx

        raw_ty = self.request.GET.get("target_year")
        try:
            target_year = int(raw_ty) if raw_ty is not None else source.year + 1
        except (TypeError, ValueError):
            target_year = source.year + 1

        from planning.services.season_rollover import summarize_skeleton

        summary = summarize_skeleton(source, target_year, dry_run=True)
        target_row = PlanningYear.objects.filter(year=target_year).first()
        target_has_plantings = (
            Planting.objects.filter(planning_year=target_row).exists() if target_row else False
        )

        ctx.update(
            {
                "source": source,
                "target_year": target_year,
                "summary": summary,
                "target_row": target_row,
                "target_has_plantings": target_has_plantings,
            }
        )
        return ctx


@method_decorator(csrf_protect, name="dispatch")
class SeasonInitBlankView(StaffPlanningMixin, View):
    """YP-2: create an empty planning year and switch session."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        try:
            target_year = int(request.POST.get("target_year", ""))
        except (TypeError, ValueError):
            messages.error(request, "Invalid target year.")
            return redirect("planning:matrix")

        target, _ = PlanningYear.objects.get_or_create(
            year=target_year,
            defaults={"status": "planning"},
        )
        set_session_planning_year(request, target)
        messages.success(
            request,
            f"Started blank planning year {target.year}. Add plantings on the Crop Planner.",
        )
        return redirect("planning:matrix")


@method_decorator(csrf_protect, name="dispatch")
class SeasonRolloverCommitView(StaffPlanningMixin, View):
    """YP-3 / YP-5: copy skeleton into target year and switch session."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        try:
            target_year = int(request.POST.get("target_year", ""))
        except (TypeError, ValueError):
            messages.error(request, "Invalid target year.")
            return redirect("planning:season_rollover_preview")

        source = get_effective_planning_year(request)
        if not source:
            messages.error(request, "No active planning year.")
            return redirect("planning:matrix")

        target, _ = PlanningYear.objects.get_or_create(
            year=target_year,
            defaults={"status": "planning"},
        )

        from planning.services.season_rollover import copy_skeleton

        try:
            result = copy_skeleton(source, target, dry_run=False)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(
                f"{reverse('planning:season_rollover_preview')}?target_year={target_year}"
            )

        set_session_planning_year(request, target)
        messages.success(request, result.message or f"Copied plan into {target.year}.")
        return redirect("planning:matrix")


class ActivePlanningYearMixin:
    """Redirect views that require an active planning year."""

    year_obj = None

    def dispatch(self, request, *args, **kwargs):
        self.year_obj = get_effective_planning_year(request)
        if not self.year_obj:
            messages.error(request, "No active planning year configured.")
            return redirect("planning:matrix")
        return super().dispatch(request, *args, **kwargs)


class SuccessionsByBlockView(ActivePlanningYearMixin, TemplateView):
    """List plantings for a crop grouped by block (succession overview)."""

    template_name = "planning/successions_by_block.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        crop_id = self.request.GET.get("crop")
        crops = active_crop_info_for_planning_year(self.year_obj)
        plantings_qs = (
            Planting.objects.filter(planning_year=self.year_obj)
            .exclude(status__in=["skipped", "failed", "revised"])
            .select_related("crop", "block", "variety_obj")
            .order_by("crop__name", "block__walk_route_order", "planned_plant_date")
        )
        if crop_id and str(crop_id).isdigit():
            plantings_qs = plantings_qs.filter(crop_id=int(crop_id))

        by_crop_map = {}
        for p in plantings_qs:
            entry = by_crop_map.setdefault(
                p.crop_id,
                {"crop": p.crop, "blocks_map": {}},
            )
            bmap = entry["blocks_map"]
            if p.block_id not in bmap:
                bmap[p.block_id] = {"block": p.block, "rows": []}
            v = _planting_display_variety_label(p)
            bmap[p.block_id]["rows"].append(
                {
                    "planting": p,
                    "variety": v or "—",
                    "beds": f"b{p.bed_start}-{p.bed_end}",
                    "succession": p.succession_group or "—",
                }
            )

        by_crop = []
        for _cid, data in sorted(by_crop_map.items(), key=lambda x: x[1]["crop"].name):
            blocks_sorted = sorted(data["blocks_map"].values(), key=lambda b: b["block"].walk_route_order)
            by_crop.append({"crop": data["crop"], "blocks": blocks_sorted})

        ctx.update(
            {
                "year": self.year_obj,
                "crops": crops,
                "selected_crop_id": int(crop_id) if crop_id and str(crop_id).isdigit() else None,
                "by_crop": by_crop,
            }
        )
        return ctx


class PlantingDetailView(DetailView):
    """HTMX partial: planting detail panel."""

    model = Planting
    template_name = "planning/planting_detail_page.html"

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["planning/partials/planting_detail.html"]
        return [self.template_name]

    def get_queryset(self):
        return super().get_queryset().select_related("crop", "crop_season", "block", "variety_obj")

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

        if p.actual_plant_date and p.planned_plant_date:
            drift_days = (p.actual_plant_date - p.planned_plant_date).days
            if abs(drift_days) > Planting.PLANT_DATE_DRIFT_DAYS:
                ctx["plant_date_drift_banner"] = drift_days

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
                "planting_form_full_reference_catalog": True,
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
    form_class = PlantingForm

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["planning/partials/planting_form.html"]
        return ["planning/planting_form_page.html"]

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
    form_class = PlantingForm

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["planning/partials/planting_form.html"]
        return ["planning/planting_form_page.html"]

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
    form_class = SuccessionForm

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["planning/partials/succession_form_inner.html"]
        return ["planning/succession_form.html"]

    def get_initial(self):
        initial = super().get_initial()
        block_id = self.request.GET.get("block")
        fpw = self.request.GET.get("first_plant_week")
        lpw = self.request.GET.get("last_plant_week")
        bt = (self.request.GET.get("block_type") or "").strip()
        if block_id and str(block_id).isdigit():
            blk = Block.objects.filter(pk=int(block_id)).first()
            if blk:
                initial["block"] = blk
                if bt not in ("field", "high_tunnel", "greenhouse"):
                    initial["block_type"] = blk.block_type
                else:
                    initial["block_type"] = bt
        if fpw is not None and str(fpw).strip().isdigit():
            try:
                initial["first_plant_week"] = int(fpw)
            except (TypeError, ValueError):
                pass
        if lpw is not None and str(lpw).strip().isdigit():
            try:
                initial["last_plant_week"] = int(lpw)
            except (TypeError, ValueError):
                pass
        elif bt in ("field", "high_tunnel", "greenhouse"):
            initial["block_type"] = bt
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            {
                "crop_choices": CropInfo.objects.all().order_by("crop_type", "name"),
                "block_choices": Block.objects.all().order_by("block_type", "walk_route_order"),
                "is_htmx": bool(self.request.headers.get("HX-Request")),
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
        if self.request.headers.get("HX-Request"):
            return HttpResponse(
                status=204,
                headers={"HX-Redirect": reverse("planning:matrix")},
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
                .select_related(
                    "planting__crop",
                    "planting__block",
                    "planting__variety_obj",
                )
                .prefetch_related("planting__nursery_events")
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
                    "sunday": sunday,
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
        tray_highlight_window = max((w["bench_trays"] for w in weeks_data), default=0)

        planting_count_excl_dead = (
            Planting.objects.filter(planning_year=self.year_obj)
            .exclude(status__in=["skipped", "failed"])
            .count()
        )
        nursery_event_year_total = NurseryEvent.objects.filter(
            planting__planning_year=self.year_obj
        ).count()
        window_total_events = sum(w["total_events"] for w in weeks_data)
        nursery_diagnostic_hints = nursery_surface_hints(
            planting_count_excl_dead=planting_count_excl_dead,
            nursery_event_year_total=nursery_event_year_total,
            window_total_events=window_total_events,
        )

        ctx.update(
            {
                "year": self.year_obj,
                "center_week": center_week,
                "weeks": weeks_data,
                "greenhouse_capacity": greenhouse_capacity,
                "bench_by_week": bench_by_week,
                "peak_week": peak_week,
                "tray_highlight_window": tray_highlight_window,
                "nursery_diagnostic_hints": nursery_diagnostic_hints,
                "window_total_events": window_total_events,
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
    """Product-by-week sales demand grid aligned to workbook rollups (Markets / Orders / CSA)."""

    template_name = "planning/sales_plan.html"
    sales_plan_mode = "rollup"  # SalesPlanByChannelView sets "by_channel"

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)

        action = request.POST.get("action", "save")
        rollup_slug = None
        channel = None
        sales_category = None

        if self.sales_plan_mode == "rollup":
            rollup_slug = (request.POST.get("rollup") or DEFAULT_ROLLUP_SLUG).strip().lower()
            if rollup_slug not in ROLLUP_SLUG_TO_CATEGORY_NAME:
                messages.error(request, "Invalid planning bucket.")
                return redirect(reverse("planning:sales_plan"))
            cat_name = ROLLUP_SLUG_TO_CATEGORY_NAME[rollup_slug]
            sales_category = SalesCategory.objects.filter(name=cat_name).first()
            if not sales_category:
                messages.error(
                    request,
                    "Sales categories are missing. Run reference data import or add "
                    "Markets / Orders / CSA rows on SalesCategory.",
                )
                return redirect(reverse("planning:sales_plan"))
        else:
            channel_id = request.POST.get("channel")
            if not channel_id:
                messages.error(request, "Channel is required.")
                return redirect(reverse("planning:sales_plan_by_channel"))
            try:
                channel = SalesChannel.objects.get(id=channel_id)
            except SalesChannel.DoesNotExist:
                messages.error(request, "Invalid channel.")
                return redirect(reverse("planning:sales_plan_by_channel"))

        if action == "save":
            focus_q = ""
            fp_raw = (request.POST.get("focus_product") or "").strip()
            if fp_raw.isdigit():
                focus_q = f"&focus_product={int(fp_raw)}"
            if self.sales_plan_mode == "rollup":
                updated = self._save_plan_rows(request, sales_category=sales_category)
                messages.success(
                    request,
                    f"Saved {updated} planned product-week rows for {sales_category.name}.",
                )
                return redirect(f"{reverse('planning:sales_plan')}?rollup={rollup_slug}{focus_q}")
            updated = self._save_plan_rows(request, channel=channel)
            messages.success(
                request,
                f"Saved {updated} planned product-week rows for {channel.name}.",
            )
            return redirect(
                f"{reverse('planning:sales_plan_by_channel')}?channel={channel.id}{focus_q}"
            )

        if action == "draft":
            from .services.sales_plan_translation import build_demand_to_supply_draft

            if self.sales_plan_mode == "rollup":
                draft = build_demand_to_supply_draft(self.year_obj, sales_category=sales_category)
            else:
                draft = build_demand_to_supply_draft(self.year_obj, channel=channel)
            ctx = self.get_context_data(
                channel=channel,
                draft=draft,
                rollup_slug=rollup_slug if self.sales_plan_mode == "rollup" else None,
                rollup_category=sales_category if self.sales_plan_mode == "rollup" else None,
            )
            messages.info(
                request,
                "Generated demand-to-supply draft recommendations from current planned demand.",
            )
            return render(request, self.template_name, ctx)

        messages.error(request, "Unsupported action.")
        if self.sales_plan_mode == "rollup":
            rs = rollup_slug or DEFAULT_ROLLUP_SLUG
            return redirect(f"{reverse('planning:sales_plan')}?rollup={rs}")
        return redirect(f"{reverse('planning:sales_plan_by_channel')}?channel={channel.id}")

    def _delete_category_plan_slice(self, sales_category: SalesCategory, product, sale_date) -> None:
        SalesEvent.objects.filter(
            entry_kind=SalesEvent.EntryKind.PLAN,
            planning_year=self.year_obj,
            product=product,
            sale_date=sale_date,
        ).filter(Q(sales_category=sales_category) | Q(channel__category=sales_category)).delete()

    def _save_plan_rows(self, request, channel=None, sales_category=None):
        updated = 0
        products = active_crop_sales_formats_for_planning_year(self.year_obj).select_related("crop")

        if self.sales_plan_mode == "rollup" and sales_category is not None:
            operational = list(
                SalesChannel.objects.filter(category=sales_category)
                .exclude(name__in=ROLLUP_PLAN_CHANNEL_NAMES)
                .order_by("allocation_priority", "name", "id")
            )
            n = len(operational)
            for product in products:
                for week in range(1, 53):
                    key = f"qty_{product.id}_{week}"
                    raw_value = (request.POST.get(key) or "").strip()
                    sale_date = Week(self.year_obj.year, week).monday()
                    if not raw_value:
                        self._delete_category_plan_slice(sales_category, product, sale_date)
                        continue
                    try:
                        quantity = Decimal(raw_value)
                    except (InvalidOperation, TypeError):
                        continue
                    if quantity <= 0:
                        self._delete_category_plan_slice(sales_category, product, sale_date)
                        continue

                    self._delete_category_plan_slice(sales_category, product, sale_date)
                    if n == 0:
                        revenue = quantity * (product.sale_price or Decimal("0"))
                        SalesEvent.objects.update_or_create(
                            entry_kind=SalesEvent.EntryKind.PLAN,
                            planning_year=self.year_obj,
                            channel=None,
                            sales_category=sales_category,
                            product=product,
                            sale_date=sale_date,
                            defaults={
                                "planned_quantity": quantity,
                                "planned_revenue": revenue,
                                "notes": "Sales plan entry (category)",
                            },
                        )
                        updated += 1
                    else:
                        for ch, amt in zip(operational, even_split_sale_units(quantity, n)):
                            if amt == 0:
                                continue
                            defaults = {
                                "planned_quantity": amt,
                                "planned_revenue": amt * (product.sale_price or Decimal("0")),
                                "notes": "Sales plan entry (allocated)",
                            }
                            if ch.category_id:
                                defaults["sales_category_id"] = ch.category_id
                            SalesEvent.objects.update_or_create(
                                entry_kind=SalesEvent.EntryKind.PLAN,
                                planning_year=self.year_obj,
                                channel=ch,
                                product=product,
                                sale_date=sale_date,
                                defaults=defaults,
                            )
                            updated += 1
            return updated

        if channel is None:
            return 0

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

                revenue = quantity * (product.sale_price or Decimal("0"))
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
        channels = operator_sales_channels()
        rollup_slug = None
        rollup_tabs = None
        rollup_category = kwargs.get("rollup_category")
        if self.sales_plan_mode == "rollup":
            rollup_slug = (kwargs.get("rollup_slug") or self.request.GET.get("rollup") or "").strip().lower()
            if rollup_slug not in ROLLUP_SLUG_TO_CATEGORY_NAME:
                rollup_slug = DEFAULT_ROLLUP_SLUG
            if rollup_category is None:
                cat_name = ROLLUP_SLUG_TO_CATEGORY_NAME[rollup_slug]
                rollup_category = SalesCategory.objects.filter(name=cat_name).first()
            channel = None
            rollup_tabs = [
                {
                    "slug": slug,
                    "label": ROLLUP_TAB_LABELS.get(slug, slug.title()),
                    "href": f"{reverse('planning:sales_plan')}?rollup={slug}",
                    "active": slug == rollup_slug,
                }
                for slug in ("markets", "orders", "csa")
            ]
        elif channel is None:
            selected_channel_id = self.request.GET.get("channel")
            channel = channels.filter(id=selected_channel_id).first() if selected_channel_id else channels.first()

        products = active_crop_sales_formats_for_planning_year(self.year_obj).order_by(
            "crop__crop_type", "crop__name", "product_name"
        )
        focus_raw = (self.request.GET.get("focus_product") or "").strip()
        sales_plan_focus_product_id = None
        if focus_raw.isdigit() and products.filter(pk=int(focus_raw)).exists():
            sales_plan_focus_product_id = int(focus_raw)
        has_in_plan_plantings = Planting.objects.filter(planning_year=self.year_obj).exclude(
            status__in=EXCLUDED_PLANTING_STATUSES
        ).exists()
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

        if self.sales_plan_mode == "rollup" and rollup_category:
            cat_rows = SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
            ).filter(
                Q(sales_category=rollup_category) | Q(channel__category=rollup_category)
            ).select_related("channel", "product", "sales_category")
            by_ops = defaultdict(Decimal)
            rollup_by_key = {}
            for row in cat_rows:
                wk = row.sale_date.isocalendar()[1]
                key = (row.product_id, wk)
                ch = row.channel
                if ch is None and row.sales_category_id:
                    rollup_by_key[key] = row
                elif ch and ch.name in ROLLUP_PLAN_CHANNEL_NAMES:
                    if key not in rollup_by_key:
                        rollup_by_key[key] = row
                elif ch:
                    by_ops[key] += row.planned_quantity or Decimal("0")
            for product in products:
                for week in weeks:
                    key = (product.id, week)
                    ops_sum = by_ops.get(key, Decimal("0"))
                    if ops_sum > 0:
                        planned_lookup[key] = SimpleNamespace(
                            planned_quantity=ops_sum,
                            planned_revenue=ops_sum * (product.sale_price or Decimal("0")),
                        )
                        summary_qty += ops_sum
                        summary_revenue += ops_sum * (product.sale_price or Decimal("0"))
                    elif key in rollup_by_key:
                        pr = rollup_by_key[key]
                        planned_lookup[key] = pr
                        summary_qty += pr.planned_quantity or Decimal("0")
                        summary_revenue += pr.planned_revenue or Decimal("0")
        elif channel:
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

        demand_totals = defaultdict(Decimal)
        all_plan = list(
            SalesEvent.objects.filter(
                entry_kind=SalesEvent.EntryKind.PLAN,
                planning_year=self.year_obj,
            ).select_related("product", "channel", "channel__category", "sales_category")
        )
        for row in plan_events_without_shadowed_rollups(all_plan):
            wk = row.sale_date.isocalendar()[1]
            demand_totals[(row.product_id, wk)] += row.planned_quantity or Decimal("0")

        harvest_totals = defaultdict(Decimal)
        for he in HarvestEvent.objects.filter(planting__planning_year=self.year_obj).exclude(
            planting__status__in=["skipped", "failed", "revised"]
        ).select_related("planting"):
            wk = he.planned_date.isocalendar()[1]
            harvest_totals[(he.planting.crop_id, wk)] += he.planned_quantity or Decimal("0")

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
                hq = product.harvest_qty_per_sale_unit or Decimal("1")
                if hq <= 0:
                    hq = Decimal("1")
                h_units = harvest_totals.get((product.crop_id, week), Decimal("0"))
                supply_sale_units = (h_units / hq).quantize(Decimal("0.01"))
                demand_all_channels = demand_totals.get((product.id, week), Decimal("0"))
                shortage = demand_all_channels > supply_sale_units + Decimal("0.0001")
                shortage_magnitude = (
                    (demand_all_channels - supply_sale_units).quantize(Decimal("0.01"))
                    if shortage
                    else Decimal("0")
                )
                supply_ratio = None
                if demand_all_channels > Decimal("0.0001"):
                    supply_ratio = (supply_sale_units / demand_all_channels).quantize(Decimal("0.0001"))
                if shortage:
                    css_class += " sales-plan-cell--shortage"
                shortage_title = (
                    f"Short by {shortage_magnitude} sale-units (demand {demand_all_channels}, "
                    f"supply ~{supply_sale_units})"
                    if shortage
                    else ""
                )
                week_cells.append(
                    {
                        "week": week,
                        "planned_quantity": plan.planned_quantity if plan else None,
                        "window_state": window_state,
                        "css_class": css_class,
                        "title": (
                            f"{window_label} · Week {week} · "
                            f"Demand(all channels) {demand_all_channels} · "
                            f"Harvest→sale-units ~{supply_sale_units}"
                            + (f" · {shortage_title}" if shortage else "")
                        ),
                        "demand_all_channels": demand_all_channels,
                        "supply_sale_units": supply_sale_units,
                        "shortage": shortage,
                        "shortage_magnitude": shortage_magnitude,
                        "supply_ratio": supply_ratio,
                        "shortage_title": shortage_title,
                    }
                )
            row_total_demand = sum((c["demand_all_channels"] for c in week_cells), Decimal("0"))
            row_total_supply = sum((c["supply_sale_units"] for c in week_cells), Decimal("0"))
            row_ratio = None
            if row_total_demand > Decimal("0.0001"):
                row_ratio = (row_total_supply / row_total_demand).quantize(Decimal("0.0001"))
            product_rows.append(
                {
                    "product": product,
                    "is_mix_product": product.is_mix_product,
                    "active_recipe": product.recipes.filter(is_active=True).first(),
                    "week_cells": week_cells,
                    "season_profile": selected_profile,
                    "season_window_label": window_label,
                    "row_total_demand": row_total_demand.quantize(Decimal("0.01")),
                    "row_total_supply": row_total_supply.quantize(Decimal("0.01")),
                    "row_ratio": row_ratio,
                }
            )

        harvest_event_year_total = HarvestEvent.objects.filter(
            planting__planning_year=self.year_obj
        ).exclude(planting__status__in=["skipped", "failed", "revised"]).count()
        missing_harvest_ct = count_plantings_missing_harvest_events(self.year_obj.id)
        product_count = products.count()
        rollup_category_ok = (self.sales_plan_mode != "rollup") or bool(rollup_category)
        sales_plan_diagnostic_hints = sales_plan_surface_hints(
            product_count=product_count,
            rollup_category_ok=rollup_category_ok,
            harvest_event_year_total=harvest_event_year_total,
            plantings_missing_harvest_events=missing_harvest_ct,
            planning_year_id=self.year_obj.id,
            planning_calendar_year=self.year_obj.year,
        )
        sales_plan_product_scope_hints = sales_plan_product_scope_explanations(
            has_in_plan_plantings=has_in_plan_plantings,
        )
        weekly_order_channel = channel or channels.first()
        weekly_order_week = date.today().isocalendar()[1]
        weekly_order_url = (
            reverse(
                "sales:weekly_channel_order",
                kwargs={"channel_id": weekly_order_channel.id, "week": weekly_order_week},
            )
            if weekly_order_channel
            else None
        )

        ctx.update(
            {
                "year": self.year_obj,
                "channels": channels,
                "channel": channel,
                "rollup_category": rollup_category,
                "product_rows": product_rows,
                "weeks": weeks,
                "summary_qty": summary_qty,
                "summary_revenue": summary_revenue,
                "draft": kwargs.get("draft"),
                "plan_mode": self.sales_plan_mode,
                "rollup_slug": rollup_slug,
                "rollup_tabs": rollup_tabs,
                "sales_plan_diagnostic_hints": sales_plan_diagnostic_hints,
                "sales_plan_product_scope_hints": sales_plan_product_scope_hints,
                "weekly_order_channel": weekly_order_channel,
                "weekly_order_week": weekly_order_week,
                "weekly_order_url": weekly_order_url,
                "sales_plan_focus_product_id": sales_plan_focus_product_id,
            }
        )
        return ctx


class SalesPlanByChannelView(SalesPlanView):
    """Weekly product plan for one operational sales channel (outlet-level)."""

    sales_plan_mode = "by_channel"


class PlantingReviseView(View):
    """Mark existing planting as revised, create a new one as its replacement.

    The original is kept for historical comparison.
    The new planting points back to the original via revision_of.
    """

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
        tpl = (
            "planning/partials/planting_form.html"
            if request.headers.get("HX-Request")
            else "planning/planting_form_page.html"
        )
        return render(request, tpl, ctx)

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


class NurseryRecordsView(ActivePlanningYearMixin, View):
    """Log actual nursery event dates and tray counts (near-term incomplete events)."""

    def get(self, request, *args, **kwargs):
        today = date.today()
        horizon = today + timedelta(days=14)
        events = (
            NurseryEvent.objects.filter(
                planting__planning_year=self.year_obj,
                planned_date__lte=horizon,
                actual_date__isnull=True,
            )
            .exclude(planting__status__in=["skipped", "failed", "revised"])
            .select_related(
                "planting",
                "planting__crop",
                "planting__block",
                "planting__variety_obj",
            )
            .prefetch_related("planting__nursery_events")
            .order_by("planned_date", "event_type", "planting__crop__name")
        )
        ctx = {
            "year": self.year_obj,
            "events": events,
            "today": today,
        }
        return render(request, "planning/nursery_records.html", ctx)

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/admin/login/?next={request.path}")
        if not request.user.is_staff:
            return HttpResponse(status=403)
        eid = request.POST.get("event_id")
        if not eid:
            messages.error(request, "Missing event.")
            return redirect("planning:nursery_records")
        ev = get_object_or_404(
            NurseryEvent.objects.select_related("planting"),
            pk=eid,
            planting__planning_year=self.year_obj,
        )
        ad = (request.POST.get("actual_date") or "").strip()
        if ad:
            try:
                ev.actual_date = date.fromisoformat(ad)
            except ValueError:
                messages.warning(request, "Invalid actual date.")
        atc = (request.POST.get("actual_tray_count") or "").strip()
        if atc:
            try:
                ev.actual_tray_count = int(atc)
            except ValueError:
                pass
        ag = (request.POST.get("actual_germination_rate") or "").strip()
        if ag:
            try:
                ev.actual_germination_rate = Decimal(ag)
            except InvalidOperation:
                pass
        notes_add = (request.POST.get("notes") or "").strip()
        if notes_add:
            ev.notes = (ev.notes + "\n" + notes_add).strip()
        ev.save()
        if ev.event_type == "seed" and ev.actual_germination_rate is not None:
            from planning.services.germination_cascade import apply_germination_cascade

            n_adj = apply_germination_cascade(ev.planting)
            if n_adj:
                messages.info(request, f"Adjusted {n_adj} future harvest week(s) for germination rate.")
        messages.success(request, "Nursery record updated.")
        return redirect("planning:nursery_records")


class NurseryTodoView(ActivePlanningYearMixin, TemplateView):
    """Upcoming incomplete nursery tasks (next 7 days)."""

    template_name = "planning/nursery_todo.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = date.today()
        horizon = today + timedelta(days=7)
        events = (
            NurseryEvent.objects.filter(
                planting__planning_year=self.year_obj,
                planned_date__gte=today,
                planned_date__lte=horizon,
                actual_date__isnull=True,
            )
            .exclude(planting__status__in=["skipped", "failed", "revised"])
            .select_related("planting", "planting__crop", "planting__block")
            .prefetch_related("planting__nursery_events")
            .order_by("planned_date", "event_type")
        )
        direct_seed_plantings = list(
            Planting.objects.filter(
                planning_year=self.year_obj,
                crop__nursery_weeks=0,
                planned_plant_date__gte=today,
                planned_plant_date__lte=horizon,
                status__in=["planned", "seeded", "planted"],
            )
            .select_related("crop", "block")
            .order_by("planned_plant_date", "block__name", "bed_start")
        )
        nursery_event_rows = []
        for ev in events:
            p = ev.planting
            nursery_event_rows.append(
                {
                    "event": ev,
                    "planting_display_id": planting_unit_code(p),
                    "schedule_chip_class": planting_schedule_chip_css_class(
                        p.planned_plant_date, p.actual_plant_date, today
                    ),
                }
            )
        direct_seed_rows = []
        for p in direct_seed_plantings:
            direct_seed_rows.append(
                {
                    "planting": p,
                    "planting_display_id": planting_unit_code(p),
                    "schedule_chip_class": planting_schedule_chip_css_class(
                        p.planned_plant_date, p.actual_plant_date, today
                    ),
                }
            )
        ctx.update(
            {
                "year": self.year_obj,
                "nursery_event_rows": nursery_event_rows,
                "today": today,
                "direct_seed_rows": direct_seed_rows,
            }
        )
        return ctx


class NurseryScheduleFullPrintView(ActivePlanningYearMixin, TemplateView):
    """Printable full-season nursery schedule."""

    template_name = "planning/nursery_schedule_full_print.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        year = self.year_obj.year
        events = (
            NurseryEvent.objects.filter(planting__planning_year=self.year_obj)
            .exclude(planting__status__in=["skipped", "failed", "revised"])
            .select_related(
                "planting",
                "planting__crop",
                "planting__block",
                "planting__variety_obj",
            )
            .prefetch_related("planting__nursery_events")
            .order_by("planned_date", "planting__crop__name", "event_type")
        )
        direct_seed_plantings = list(
            Planting.objects.filter(planning_year=self.year_obj, crop__nursery_weeks=0)
            .exclude(status__in=["skipped", "failed", "revised"])
            .select_related("crop", "block", "variety_obj")
            .order_by("planned_plant_date", "crop__name", "block__name", "bed_start")
        )
        events_list = list(events)
        planting_ids = {e.planting_id for e in events_list}
        seed_week_by_planting = {}
        if planting_ids:
            for pid, pdate in NurseryEvent.objects.filter(
                planting_id__in=planting_ids, event_type="seed"
            ).values_list("planting_id", "planned_date"):
                if pdate is not None:
                    seed_week_by_planting[int(pid)] = pdate.isocalendar()[1]
        for e in events_list:
            e.seeding_week_iso = seed_week_by_planting.get(e.planting_id)
        ctx.update(
            {
                "year": self.year_obj,
                "plan_year": year,
                "events": events_list,
                "direct_seed_plantings": direct_seed_plantings,
            }
        )
        return ctx
