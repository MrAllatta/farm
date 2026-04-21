"""Aggregate seed needs from plantings for the seed order report."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from planning.models import Planting


def planting_variety_label(p: Planting) -> str:
    if getattr(p, "variety_obj_id", None) and p.variety_obj:
        return p.variety_obj.name
    return (p.variety or "").strip() or "—"


def build_seed_order_rows(
    plantings: list[Planting],
    overplant: float,
) -> list[dict[str, Any]]:
    """Group plantings by (crop_id, variety_key) and compute seed math."""
    groups: dict[tuple[int, str], dict[str, Any]] = {}

    for p in plantings:
        crop = p.crop
        cs = p.crop_season
        vkey = planting_variety_label(p)
        key = (crop.id, vkey)
        if key not in groups:
            groups[key] = {
                "crop": crop,
                "variety_label": vkey,
                "variety_obj": getattr(p, "variety_obj", None),
                "total_bedfeet": 0,
                "plantings": [],
                "crop_season": cs,
            }
        groups[key]["total_bedfeet"] += p.planned_bedfeet
        groups[key]["plantings"].append(p)
        # Prefer first planting's season profile for calc (same as legacy)
        groups[key]["crop_season"] = groups[key]["plantings"][0].crop_season

    seed_orders = []
    for _key, data in groups.items():
        crop = data["crop"]
        cs = data["crop_season"]
        total_bf = data["total_bedfeet"]
        result = _calculate_seeds(crop, cs, total_bf, overplant)
        result["crop"] = crop
        result["variety_label"] = data["variety_label"]
        result["variety_obj"] = data.get("variety_obj")
        result["total_bedfeet"] = total_bf
        result["num_plantings"] = len(data["plantings"])
        seed_orders.append(result)

    seed_orders.sort(
        key=lambda x: (
            0 if x["method"] == "direct_seed" else 1 if x["method"] == "transplant" else 2,
            x["crop"].name,
            x["variety_label"],
        )
    )
    return seed_orders


def _calculate_seeds(crop, crop_season, total_bedfeet: int, overplant: float) -> dict[str, Any]:
    if crop.propagation_type != "seed":
        return _calc_vegetative(crop, crop_season, total_bedfeet, overplant)

    if crop_season.ds_seed_rate:
        return _calc_direct_seed(crop, crop_season, total_bedfeet, overplant)

    if crop_season.tp_inrow_spacing:
        return _calc_transplant(crop, crop_season, total_bedfeet, overplant)

    return {
        "method": "unknown",
        "seeds_needed": 0,
        "ounces_needed": None,
        "order_rounded": "?",
        "calculation": "Missing seed rate and spacing data",
    }


def _calc_direct_seed(crop, cs, total_bf: int, overplant: float) -> dict[str, Any]:
    rows = cs.rows_per_bed or 1
    rate = cs.ds_seed_rate
    seeds = total_bf * rows * rate * overplant
    ounces = None
    order = None
    if crop.seeds_per_ounce and crop.seeds_per_ounce > 0:
        ounces = seeds / float(crop.seeds_per_ounce)
        order = _round_order(ounces)
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


def _calc_transplant(crop, cs, total_bf: int, overplant: float) -> dict[str, Any]:
    rows = cs.rows_per_bed or 1
    spacing = float(cs.tp_inrow_spacing)
    plants = total_bf * rows / spacing * overplant
    seeds_per_cell = crop.seeds_per_cell or 1
    thinned = crop.thinned_plants or 0
    if thinned > 0 and seeds_per_cell > 1:
        cells = plants
    else:
        cells = plants
    seeds = cells * seeds_per_cell
    trays = None
    if crop.seeded_tray_size and crop.seeded_tray_size > 1:
        trays = math.ceil(cells / crop.seeded_tray_size)
    ounces = None
    order = None
    if crop.seeds_per_ounce and crop.seeds_per_ounce > 0:
        ounces = seeds / float(crop.seeds_per_ounce)
        order = _round_order(ounces)
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


def _calc_vegetative(crop, cs, total_bf: int, overplant: float) -> dict[str, Any]:
    rows = cs.rows_per_bed or 1
    spacing = float(cs.tp_inrow_spacing) if cs.tp_inrow_spacing else 1
    pieces = total_bf * rows / spacing * overplant
    weight_per_piece = {
        "vegetative_clove": 60,
        "vegetative_tuber": 2,
        "vegetative_slip": None,
    }
    pcs_per_lb = weight_per_piece.get(crop.propagation_type)
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


def _round_order(ounces: float | None) -> str:
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
    lbs = ounces / 16
    if lbs < 1:
        return f"{math.ceil(ounces)} oz ({lbs:.1f} lb)"
    return f"{math.ceil(lbs)} lb"
