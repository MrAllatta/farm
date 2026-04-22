"""Shared week-scoped context for Field Walk, Harvest Needs, and Record Harvest."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from django.db.models import Prefetch

from isoweek import Week

from operations.models import FieldWalkNote, InventoryLedger
from planning.models import HarvestEvent, Planting, PlantingStatus
from reference.sales_rollups import plan_events_without_shadowed_rollups
from sales.models import SalesEvent

Mode = Literal["field_walk", "harvest_needs", "harvest_entry"]

ACTIVE_FIELD_STATUSES = (
    PlantingStatus.PLANTED,
    PlantingStatus.GROWING,
    PlantingStatus.HARVESTING,
)

EXCLUDED_PLANTING_STATUSES = (
    PlantingStatus.SKIPPED,
    PlantingStatus.FAILED,
    PlantingStatus.REVISED,
)


def week_bounds_for_planning_year(calendar_year: int, iso_week: int) -> tuple[date, date]:
    """Monday–Sunday for ISO week, clamped to 1–52 (matches existing views)."""
    wk = max(1, min(52, iso_week))
    monday = Week(calendar_year, wk).monday()
    sunday = monday + timedelta(days=6)
    return monday, sunday


def variety_label(planting: Planting) -> str:
    if getattr(planting, "variety_obj_id", None) and planting.variety_obj:
        return planting.variety_obj.name
    return (planting.variety or "").strip()


def expected_stage(planting: Planting, today: date) -> str:
    """Human-readable growth stage (same logic as legacy FieldWalkView)."""
    plant_date = planting.actual_plant_date or planting.planned_plant_date
    days_since_plant = (today - plant_date).days if plant_date else 0
    weeks_since_plant = days_since_plant // 7
    dtm = planting.crop_season.dtm_days
    harvest_start = planting.actual_first_harvest_date or planting.planned_first_harvest_date

    if today >= harvest_start:
        weeks_harvesting = (today - harvest_start).days // 7
        return f"Harvesting (week {weeks_harvesting + 1} of {planting.crop_season.harvest_weeks})"
    if days_since_plant > dtm * 0.75:
        return f"Approaching harvest ({weeks_since_plant}wk, DTM {dtm}d)"
    if days_since_plant > dtm * 0.5:
        return f"Mid-growth ({weeks_since_plant}wk)"
    return f"Establishing ({weeks_since_plant}wk)"


def target_bins_for_event(he: HarvestEvent) -> float | None:
    crop = he.planting.crop
    upb = crop.units_per_bin
    if not upb:
        return None
    try:
        pq = float(he.planned_quantity)
    except (TypeError, ValueError):
        return None
    return pq / float(upb)


def actual_bins_for_event(he: HarvestEvent) -> float | None:
    if he.actual_bins is not None:
        return float(he.actual_bins)
    return None


def latest_note_from_prefetched(planting: Planting) -> FieldWalkNote | None:
    """Use prefetched ``field_walk_notes`` (ordered newest first) when present."""
    for n in planting.field_walk_notes.all():
        return n
    return None


def days_since_walk(note: FieldWalkNote | None, today: date) -> int | None:
    if note is None:
        return None
    return (today - note.walk_date).days


def inventory_balance_for_crop(crop_id: int) -> Decimal:
    latest = (
        InventoryLedger.objects.filter(crop_id=crop_id)
        .order_by("-event_date", "-created_at", "-id")
        .first()
    )
    return latest.running_balance if latest else Decimal("0")


def week_context(
    planning_year,
    iso_week: int,
    *,
    today: date | None = None,
    mode: Mode = "harvest_needs",
) -> dict[str, Any]:
    """
    Build ordered block → planting rows with walk metadata and harvest events in week.

    ``mode``:
    - ``field_walk``: all active plantings (walk route); each row may have zero harvest events.
    - ``harvest_needs`` / ``harvest_entry``: only plantings with at least one harvest event in week.
    """
    today = today or date.today()
    year = planning_year.year
    week_monday, week_sunday = week_bounds_for_planning_year(year, iso_week)

    notes_prefetch = Prefetch(
        "field_walk_notes",
        queryset=FieldWalkNote.objects.order_by("-walk_date", "-id"),
    )

    if mode == "field_walk":
        plantings_qs = (
            Planting.objects.filter(
                planning_year=planning_year,
                status__in=ACTIVE_FIELD_STATUSES,
            )
            .select_related("crop", "crop_season", "block", "planning_year", "variety_obj")
            .prefetch_related(
                notes_prefetch,
                Prefetch(
                    "harvest_events",
                    queryset=HarvestEvent.objects.filter(
                        planned_date__gte=week_monday,
                        planned_date__lte=week_sunday,
                    ).order_by("planned_date"),
                ),
            )
            .order_by(
                "block__walk_route_order",
                "block__name",
                "bed_start",
            )
        )
        plantings = list(plantings_qs)
    else:
        he_qs = (
            HarvestEvent.objects.filter(
                planting__planning_year=planning_year,
                planned_date__gte=week_monday,
                planned_date__lte=week_sunday,
            )
            .exclude(planting__status__in=EXCLUDED_PLANTING_STATUSES)
            .select_related(
                "planting",
                "planting__crop",
                "planting__crop_season",
                "planting__block",
                "planting__planning_year",
                "planting__variety_obj",
            )
            .order_by(
                "planting__block__walk_route_order",
                "planting__block__name",
                "planting__bed_start",
                "planned_date",
            )
        )
        events = list(he_qs)
        planting_ids = {ev.planting_id for ev in events}
        extra_plantings = {}
        if planting_ids:
            for p in (
                Planting.objects.filter(pk__in=planting_ids)
                .select_related("crop", "crop_season", "block", "planning_year", "variety_obj")
                .prefetch_related(notes_prefetch)
            ):
                extra_plantings[p.pk] = p
        planting_by_id: dict[int, Planting] = {}
        events_by_planting: dict[int, list[HarvestEvent]] = defaultdict(list)
        for ev in events:
            pid = ev.planting_id
            if pid not in planting_by_id:
                planting_by_id[pid] = extra_plantings.get(pid) or ev.planting
            events_by_planting[pid].append(ev)
        ordered_pids = []
        seen = set()
        for ev in events:
            if ev.planting_id not in seen:
                seen.add(ev.planting_id)
                ordered_pids.append(ev.planting_id)
        plantings = [planting_by_id[pid] for pid in ordered_pids]
        for p in plantings:
            p._week_ops_events = events_by_planting[p.pk]  # type: ignore[attr-defined]

    blocks_out: list[dict[str, Any]] = []
    block_index: dict[int, int] = {}

    def ensure_block(blk) -> dict[str, Any]:
        bid = blk.id
        if bid not in block_index:
            block_index[bid] = len(blocks_out)
            blocks_out.append({"block": blk, "plantings": []})
        return blocks_out[block_index[bid]]

    week_events_for_progress: list[HarvestEvent] = []

    sale_events = list(
        SalesEvent.objects.filter(
            planning_year=planning_year,
            entry_kind=SalesEvent.EntryKind.PLAN,
            sale_date__gte=week_monday,
            sale_date__lte=week_sunday,
        ).select_related("channel", "channel__category", "product", "product__crop")
    )
    sale_events = plan_events_without_shadowed_rollups(sale_events)

    for p in plantings:
        blk = p.block
        bucket = ensure_block(blk)
        note = latest_note_from_prefetched(p)
        y_pct = note.yield_adjust_pct if note else 100
        if mode == "field_walk":
            week_events = [e for e in p.harvest_events.all()]
        else:
            week_events = list(getattr(p, "_week_ops_events", []))
        week_events_for_progress.extend(week_events)

        target_bins_sum = Decimal("0")
        for he in week_events:
            tb = target_bins_for_event(he)
            if tb is not None:
                target_bins_sum += Decimal(str(tb))

        row = {
            "planting": p,
            "variety_label": variety_label(p),
            "last_walk_note": note,
            "days_since_walk": days_since_walk(note, today),
            "yield_adjust_pct": y_pct,
            "harvest_events_this_week": week_events,
            "expected_stage": expected_stage(p, today),
            "target_bins_week_sum": target_bins_sum,
            "harvest_start": p.actual_first_harvest_date or p.planned_first_harvest_date,
        }
        bucket["plantings"].append(row)

    total_events = len(week_events_for_progress)
    recorded_events = sum(
        1 for he in week_events_for_progress if he.actual_quantity is not None
    )

    by_crop: dict[int, dict[str, Any]] = {}
    for he in week_events_for_progress:
        crop = he.planting.crop
        cid = crop.id
        if cid not in by_crop:
            by_crop[cid] = {
                "crop": crop,
                "target_bins": Decimal("0"),
                "actual_bins": Decimal("0"),
                "planned_qty": Decimal("0"),
                "actual_qty": Decimal("0"),
                "event_count": 0,
                "recorded_count": 0,
            }
        entry = by_crop[cid]
        entry["event_count"] += 1
        entry["planned_qty"] += he.planned_quantity or Decimal("0")
        tb = target_bins_for_event(he)
        if tb is not None:
            entry["target_bins"] += Decimal(str(tb))
        if he.actual_quantity is not None:
            entry["recorded_count"] += 1
            entry["actual_qty"] += he.actual_quantity
        ab = actual_bins_for_event(he)
        if ab is not None:
            entry["actual_bins"] += Decimal(str(ab))

    by_channel: dict[int, dict[str, Any]] = {}
    for se in sale_events:
        ch = se.channel
        if ch.id not in by_channel:
            by_channel[ch.id] = {"channel": ch, "planned_qty": Decimal("0"), "rows": []}
        pq = se.planned_quantity or Decimal("0")
        by_channel[ch.id]["planned_qty"] += pq
        crop_name = ""
        if se.product_id and se.product and se.product.crop_id:
            crop_name = se.product.crop.name
        by_channel[ch.id]["rows"].append(
            {
                "product_name": se.product.product_name if se.product_id else "—",
                "crop_name": crop_name,
                "planned_quantity": pq,
                "sale_unit": se.product.sale_unit if se.product_id else "",
            }
        )

    by_sales_category: dict[str, dict[str, Any]] = {}
    category_sort = {"Markets": 1, "Orders": 2, "CSA": 3}
    for se in sale_events:
        ch = se.channel
        cat = getattr(ch, "category", None)
        if not cat:
            continue
        label = str(cat.name)
        if label not in by_sales_category:
            by_sales_category[label] = {
                "label": label,
                "planned_qty": Decimal("0"),
                "sort": category_sort.get(label, 99),
            }
        by_sales_category[label]["planned_qty"] += se.planned_quantity or Decimal("0")
    week_rollup_by_sales_category = sorted(
        by_sales_category.values(),
        key=lambda row: (row["sort"], row["label"]),
    )

    demand_by_crop: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"qty": Decimal("0"), "sale_unit": "", "label": ""}
    )
    for se in sale_events:
        if not se.product_id or not se.product or not se.product.crop_id:
            continue
        crop = se.product.crop
        cid = crop.id
        pq = se.planned_quantity or Decimal("0")
        demand_by_crop[cid]["qty"] += pq
        demand_by_crop[cid]["sale_unit"] = se.product.sale_unit or ""
        demand_by_crop[cid]["label"] = crop.name

    return {
        "planning_year": planning_year,
        "iso_week": max(1, min(52, iso_week)),
        "week_monday": week_monday,
        "week_sunday": week_sunday,
        "today": today,
        "blocks": blocks_out,
        "week_rollup_by_crop": by_crop,
        "week_rollup_by_channel": by_channel,
        "week_rollup_by_sales_category": week_rollup_by_sales_category,
        "sales_demand_by_crop": dict(demand_by_crop),
        "progress": {
            "total_events": total_events,
            "recorded_events": recorded_events,
        },
    }


def crop_variance_for_week(planning_year, iso_week: int) -> list[dict[str, Any]]:
    """Per-crop target vs actual bins for harvest events in the ISO week."""
    ctx = week_context(
        planning_year,
        iso_week,
        mode="harvest_entry",
    )
    out = []
    for _cid, payload in sorted(
        ctx["week_rollup_by_crop"].items(),
        key=lambda x: x[1]["crop"].name.lower(),
    ):
        crop = payload["crop"]
        tgt = payload["target_bins"]
        act = payload["actual_bins"]
        if tgt == 0 and act == 0:
            continue
        pct = None
        if tgt > 0:
            pct = float((act - tgt) / tgt * 100) if tgt else None
        out.append(
            {
                "crop": crop,
                "target_bins": tgt,
                "actual_bins": act,
                "variance_pct": pct,
            }
        )
    return out
